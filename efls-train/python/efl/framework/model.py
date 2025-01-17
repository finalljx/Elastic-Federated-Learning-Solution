# Copyright (C) 2016-2021 Alibaba Group Holding Limited
# 
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import copy
import contextlib
import multiprocessing
import collections

import tensorflow.compat.v1 as tf

from tensorflow.python.training import monitored_session
from tensorflow.python.platform import tf_logging
from tensorflow.python.framework import ops
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import control_flow_ops
from tensorflow.python.ops import gradients_impl
from tensorflow.python.ops import io_ops

from efl import exporter
from efl.hooks import logger_hook
from efl.framework.stage import Stage
from efl.framework.hook_manager import get_hook_manager
from efl.framework.stage_manager import StageManager
from efl.framework import task_scope
from efl.framework import session_patch
from efl.framework.communicator import Communicator
from efl.framework.common_define import *
from efl.framework import context
from efl.utils import column_util, communicator_util
from efl.utils import config
from efl.utils import func_patcher
from efl.utils import slice_op

tf.logging.set_verbosity(tf.logging.INFO)

_DEFAULT_OPT_CONFIG = {
  'REDUCE': 'mean',
  'BACKEND_MODE': 'noise'
}

@exporter.export("Model")
class Model(object):
  r'''abstraction for user defined model'''
  def __init__(self):
    self._inputs = {}
    self._input_fns = {}
    self._losses = {}
    self._loss_fns = {}
    self._train_ops = {}
    self._opt_fns = {}
    self._eval_ops = {}
    self._eval_fns = {}
    self._opt_to_vars = {}
    self._metrics = {}
    self._hook_mgr = get_hook_manager()
    self._stage_mgr = None
    self._is_training = tf.placeholder(
      tf.bool,
      shape=[],
      name="is_training_flag")
    self._metric_variables_initializer = []
    self._extra_data = {}

  @property
  def training_flag(self):
    return self._is_training

  @property
  def global_step(self):
    return tf.train.get_or_create_global_step()

  @property
  def input_fns(self):
    return self._input_fns

  @property
  def loss_fns(self):
    return self._loss_fns

  @property
  def opt_fns(self):
    return self._opt_fns

  @property
  def losses(self):
    return self._losses

  @property
  def inputs(self):
    return self._inputs

  @property
  def train_ops(self):
    return self._train_ops

  @property
  def extra_data(self):
    return self._extra_data

  @property
  def metric_variables_initializer(self):
    return self._metric_variables_initializer

  @property
  def stage_mgr(self):
    return self._stage_mgr

  def _get_task_scope(self, mode, task):
    ts = task_scope.current_task_scope()
    if mode:
      ts._mode = mode
    if task:
      ts._task = task
    if ts.mode is None:
      raise ValueError("must have mode param")
    return ts

  def input(self, mode=MODE.TRAIN, task=None):
    ts = self._get_task_scope(mode, task)
    if ts not in self._inputs:
      raise ValueError('get input failed for mode:{} task:{}'.format(mode, task))
    else:
      return self._inputs[ts]

  def metrics(self, mode=MODE.TRAIN, task=None):
    ts = self._get_task_scope(mode, task)
    if ts not in self._metrics:
      return []
    return self._metrics[ts]

  def add_extra_data(self, key, value):
    if key in self._extra_data:
      raise ValueError("key[{}] already exist".format(key))
    self._extra_data[key] = value

  def get_extra_data(self, key):
    if key not in self._extra_data:
      return None
    return self._extra_data[key]

  def add_train_op(self, train_op, task=None):
    if task not in self._train_ops:
      self._train_ops[task] = []
    self._train_ops[task].append(train_op)

  def add_eval_op(self, eval_op, task=None):
    if task not in self._eval_ops:
      self._eval_ops[task] = []
    self._eval_ops[task].append(eval_op)

  def loss(self, task=None):
    task = task if task else task_scope.current_task_scope().task
    if task not in self._losses:
      raise ValueError('get loss failed for task:{}'.format(task))
    else:
      return self._losses[task]

  def train_op(self, task=None):
    task = task if task else task_scope.current_task_scope().task
    if task not in self._train_ops:
      raise ValueError('get train_op failed for task:{}'.format(task))
    else:
      return self._train_ops[task]

  def eval_op(self, task=None):
    task = task if task else task_scope.current_task_scope().task
    if task not in self._eval_ops:
      raise ValueError('get eval_op failed for task:{}'.format(task))
    else:
      return self._eval_ops[task]

  def opt_to_vars(self, task=None):
    task = task if task else task_scope.current_task_scope().task
    if task not in self._opt_to_vars:
      raise RuntimeError('get opt_to_vars failed for task:{}'.format(task))
    return self._opt_to_vars[task]

  r'''model interface'''
  def add_hooks(self, hooks, mode=MODE.TRAIN, task=None):
    r'''add hooks under specified task and mode
    Args:
      hooks: list of tf.train.SessionRunHook
      mode: efl.MODE.TRAIN or efl.MODE.EVAL
      task: task_name, None for single task training
    '''
    ts = self._get_task_scope(mode, task)
    self._hook_mgr.add_hooks(hooks, ts.mode, ts.task)

  def add_metric(self, name, metric, mode=MODE.TRAIN, task=None):
    r'''add metric which will be printed by LoggerHook
    Args:
      name: metric name
      metric: a tensorflow op
      mode: efl.MODE.TRAIN or efl.MODE.EVAL
      task: task_name, None for single task training
    '''
    ts = self._get_task_scope(mode, task)
    if ts not in self._metrics:
      self._metrics[ts] = {}
    self._metrics[ts][name] = metric

  def input_fn(self, input_fn, task=None):
    r'''construct input fn under specified task and mode
    Args:
      input_fn: a python function defines dense features and
                sparse embeddings, return a efl.Sample object
      task: task_name, None for single task training
    '''
    task = task if task else task_scope.current_task_scope().task
    if task in self._input_fns:
      raise ValueError("input define twice for task:{}".format(task))
    self._input_fns[task] = input_fn
    return self

  def loss_fn(self, loss_fn, task=None):
    r'''define dense model function, return loss
    Args:
      loss_fn: construct loss from a efl.Sample
        function signature:
          def loss_fn(model, sample):
            ...
            return loss
          params:
            model: efl.Model instance
            sample: efl.Sample instance
      task: task_name, None for single task training
    '''
    task = task if task else task_scope.current_task_scope().task
    if task in self._loss_fns:
      raise ValueError("loss define twice for task:{}".format(task))
    self._loss_fns[task] = loss_fn
    return self

  def optimizer_fn(self, optimizer_fn, task=None):
    r'''define optimizer, return optimizer to variable dict
    Args:
      optimizer_fn: construct optimizers for variables
        function signature:
          def optimizer_fn(model):
            opt_to_vars = {}
            opt_to_vars[tf.train.AdamOptimizer(0.01)] = tf.trainable_variables()
            return opt_to_vars
      task: task_name, None for single task training
    '''
    task = task if task else task_scope.current_task_scope().task
    if task in self._opt_fns:
      raise ValueError('optimizer define twice for task:{}'.format(task))
    self._opt_fns[task] = optimizer_fn
    return self

  def eval_fn(self, eval_fn, task=None):
    r'''define ops for evaluation
    Args:
      eval_fn: construct evaluation ops
        function signature:
          def eval_fn(model, sample):
            ...
            return auc
          params:
            model: efl.Model instance
            sample: efl.Sample instance
      task: task_name, None for single task training
    '''
    task = task if task else task_scope.current_task_scope().task
    if task in self._eval_fns:
      raise ValueError("eval fn define twice for task:{}".format(task))
    self._eval_fns[task] = eval_fn
    return self

  def run_stage(self, name, stage_or_func, *args, **kwargs):
    r'''stage is an abstraction for a failoverable and synchronized procedure
        for example, a typical onling learning procedure may define like this:
          ==> load_from_checkpint
          ==> while True:
                train for a time window
                evalation for a time window
                save_checkpoint
        when cheif worker do restore, other worker must wait, same
        for other stages: train/evaluation/save_checkpoint
        If a worker failover in the third train stage, it should
        escape the first two when it restarts
        Stage can help do this easily, you can write code like this:
        define four efl.Stage or stage functions for restore/train/evaluation/save
        then write a procedure_fn like this:
          def procedure_fn(model):
            model.run_stage(restore_stage)
            while not finished:
              model.run_stage(train_stage)
              model.run_stage(evalation_stage)
              model.run_stage(save_stage)
    Args:
      name: name for the stage
      stage_or_func: a efl.Stage object or a function
        you can derive efl.Stage class and overwrite run function
        or pass a function signature like: def stage_func(sess)
      *args and **kwargs: params passed to Stage.run(*args, **kwargs)
    '''
    if not callable(stage_or_func):
      raise ValueError("stage_or_func must be a stage or function")
    if isinstance(stage_or_func, Stage):
      stage_or_func = stage_or_func(*args, **kwargs)
    self._stage_mgr.stage(name, stage_or_func, STAGE_CHECK_INTERVAL, *args, **kwargs)

  def _reduce_loss(self, loss, reduce_func):
    if loss is None:
      return None
    if reduce_func == 'mean':
      loss = tf.reduce_mean(loss)
    elif reduce_func == 'sum':
      loss = tf.reduce_sum(loss)
    elif callable(reduce_func):
      loss = reduce_func(loss)
    else:
      tf.logging.warn('No such reduce function called \'{}\', it will use mean by default.'.format(str(reduce_func)))
      loss = tf.reduce_mean(loss)
    return loss

  def _minimize(self, task, loss, vars_to_compute, opt, **kwargs):
    # get all vars to compute grad
    opt_config = kwargs.pop('opt_config', _DEFAULT_OPT_CONFIG)
    if 'opt_config' not in opt.compute_gradients.__code__.co_varnames:
      loss = self._reduce_loss(loss, opt_config.pop('REDUCE', 'mean'))
    var_list = vars_to_compute[0]
    var_scope = vars_to_compute[1]
    grads_and_vars = opt.compute_gradients(loss, var_list)
    return opt.apply_gradients(grads_and_vars)

  def _internal_compile(self, **kwargs):
    protocol = kwargs.pop("comm_protocol", "grpc")
    session_config = kwargs.pop("session_config", None)
    sync_optimizer_config = kwargs.pop("sync_optimizer_config", None)
    self._ctx = context.simple_context(
      session_config=session_config, 
      federal_role=None if not hasattr(self, 'federal_role') else self.federal_role,
      communicator=None if not hasattr(self, 'communicator') else self.communicator,
      protocol=protocol)
    with self._ctx.scope():
      tf.train.get_or_create_global_step()
      from efl.framework.sample import Sample, FederalSample
      for task, input_fn in self.input_fns.items():
        with task_scope.task_scope(task=task, mode=MODE.TRAIN):
          train_sample = input_fn(self, MODE.TRAIN)
        if not isinstance(train_sample, (Sample, FederalSample)):
          raise ValueError('input_fn must return a efl.Sample or efl.FederalSample for TRAIN')
        self.inputs[task_scope.TaskScope(MODE.TRAIN, task)] = train_sample
        with task_scope.task_scope(task=task, mode=MODE.EVAL):
          eval_sample = input_fn(self, MODE.EVAL)
        if eval_sample:
          if not isinstance(eval_sample, (Sample, FederalSample)):
            raise ValueError('input_fn must return a efl.Sample or efl.FederalSample for EVAL')
          self.inputs[task_scope.TaskScope(MODE.EVAL, task)] = eval_sample

      for task, loss_fn in self.loss_fns.items():
        task_sample = self.input(MODE.TRAIN, task)
        with ops.control_dependencies(task_sample.before_step_ops()):
          with task_scope.task_scope(task=task, mode=MODE.TRAIN):
            loss = loss_fn(self, task_sample)
          self.losses[task] = loss
          if task not in self.opt_fns:
            raise RuntimeError('optimizer fn not define for task[{}]'.format(task))
          opt_to_vars = self.opt_fns[task](self, task)
          self._opt_to_vars[task] = opt_to_vars
          optimize_ops = []
          for opt, vars_to_compute in opt_to_vars.items():
            if sync_optimizer_config is not None:
              cfg = copy.deepcopy(sync_optimizer_config)
              opt = tf.train.SyncReplicasOptimizer(
                opt,
                cfg.pop("replicas_to_aggregate", config.get_worker_num()),
                **cfg)
              self.add_hooks(
                [opt.make_session_run_hook(config.is_chief(), num_tokens=0)],
                mode=MODE.TRAIN,
                task=task)
            optimize_ops.append(self._minimize(task, loss, vars_to_compute, opt, **kwargs))
          with ops.control_dependencies(optimize_ops):
            add_global_step_op = self.global_step.assign_add(1)
          self.add_train_op(add_global_step_op, task)
        if task in self._eval_fns:
          eval_sample = self.input(MODE.EVAL, task)
          with ops.control_dependencies(eval_sample.before_step_ops()):
            with task_scope.task_scope(task=task, mode=MODE.TRAIN):
              eval_op = self._eval_fns[task](self, eval_sample)
          if eval_op is not None:  
            self.add_eval_op(eval_op, task)

  def compile(self, **kwargs):
    r'''build model ops
    Args:
      session_config: a tf.ConfigProto contains user-specific options
      sync_optimizer_config: a config dict for tf.train.SyncReplicasOptimizer, 
                             used in sync training
      opt_config: a config for optimizer, motify this to change reduction
                  method and decide to whether or not add noise , default value is:
        {
          'REDUCE': 'mean',
          'BACKEND_MODE': 'noise'
        }
    '''
    ops.add_to_collection(COMPILE_ARGS, kwargs)
    self._internal_compile(**kwargs)
    self._metric_variables_initializer = control_flow_ops.group([
        v.initializer
        for v in ops.get_collection(ops.GraphKeys.METRIC_VARIABLES)])
    return self

  def _internal_fit(self, procedure_fn, **kwargs):
    kwargs["master"] = self._ctx.session_master
    kwargs["is_chief"] = config.is_chief()
    kwargs["config"] = self._ctx.session_config
    # user should add checkpoint/summary hook under suitable scope manually
    kwargs["save_checkpoint_secs"] = None
    kwargs["save_checkpoint_steps"] = None
    kwargs["save_summaries_secs"] = None
    kwargs["save_summaries_steps"] = None

    from tensorflow.python.training.monitored_session import _HookedSession, \
      _WrappedSession, _MonitoredSession, _RecoverableSession, _CoordinatedSession
    with func_patcher.scope():
      with monitored_session.MonitoredTrainingSession(**kwargs) as sess:
        self.stage_mgr.set_monitored_sess(sess)
        procedure_fn(self)

  def fit(self,
          procedure_fn,
          log_step = 100,
          project_name = "default_prj",
          **kwargs):
    r'''
    Args:
      procedure_fn: a python function define training procedure
      log_step: print interval for LoggerHook
      project_name: prefix for stage failover variables,
                    should set different project_name if you do
                    not want to restore it from checkpoint.
                    for example, run an auc task must set a defferent
                    project name from train
    '''
    session_patch.patch()
    self._add_logger_hook(log_step)
    self._create_stage_mgr(project_name)
    self._internal_fit(procedure_fn, **kwargs)

  r'''private functions'''
  def _create_stage_mgr(self, prj_name):
    if config.dist_mode():
      device = "/job:scheduler/task:0/CPU:0"
      self._stage_mgr = StageManager(
        root_scope = tf.get_variable_scope(),
        device = device,
        worker_id = config.get_task_index(),
        worker_num = config.get_worker_num(),
        project_name = prj_name,
        name = "stage_manager")
    else:
      self._stage_mgr = StageManager(
        root_scope = tf.get_variable_scope(),
        device = "/CPU:0",
        worker_id = 0,
        worker_num = 1,
        project_name = prj_name,
        name = "stage_manager")
    self._hook_mgr.add_sess_callback(self._stage_mgr.init_arg)

  def _add_logger_hook(self, log_step):
    for ts, metrics in self._metrics.items():
      with task_scope.task_scope(ts.mode, ts.task):
        h = logger_hook.LoggerHook(
          tf.train.get_or_create_global_step(),
          log_step)
        for name, metric in metrics.items():
          h.add_metrics(name, metric)
        self.add_hooks([h], ts.mode)

@exporter.export("FederalModel")
class FederalModel(Model):
  r'''abstraction for user defined model'''
  def __init__(self):
    self._federal_role = config.get_federal_role()
    if self._federal_role not in ('leader', 'follower'):
      raise ValueError("federal_role must be set one of [leader/follower] in FederalModel.")
    self._communicator = Communicator(config.get_federal_role(),
                                      config.get_task_index(),
                                      config.get_worker_num(),
                                      peer_addr=config.get_peer_addr(),
                                      local_addr=config.get_local_addr())
    self._recv_grad_ops = collections.defaultdict(list)
    self._require_grad_ops = collections.defaultdict(list)
    super(FederalModel, self).__init__()
    self._add_communicator_hook()

  @property
  def recv_grad_ops(self):
    return self._recv_grad_ops

  @property
  def require_grad_ops(self):
    return self._require_grad_ops

  @property
  def federal_role(self):
    return self._federal_role

  @property
  def communicator(self):
    return self._communicator

  def _add_communicator_hook(self):
    self.add_hooks([self._communicator.hook])

  def send(self, name, tensor, require_grad=False, mode=MODE.TRAIN, task=None):
    task = task if task else task_scope.current_task_scope().task
    send_op = self._communicator.send(name, tensor)
    if mode == MODE.TRAIN:
      self.add_train_op(send_op, task=task)
    else:
      self.add_eval_op(send_op, task=task)
    if require_grad:
      recv_grad = self.recv(name + '_grad', tensor.dtype)
      if task not in self._require_grad_ops:
        self._require_grad_ops[task] = []
      self._require_grad_ops[task].append((tensor, recv_grad))
    return send_op

  def recv(self, name, dtype=tf.float32, require_grad=False, task=None):
    task = task if task else task_scope.current_task_scope().task
    recv_tensor = self._communicator.recv(name, dtype)
    if require_grad:
      if task not in self._recv_grad_ops:
        self._recv_grad_ops[task] = []
      self._recv_grad_ops[task].append((name, recv_tensor))
    return recv_tensor

  def _minimize(self, task, loss, vars_to_compute, opt, **kwargs):
    var_list = vars_to_compute[0]
    var_scope = vars_to_compute[1]
    recv_grads = communicator_util.get_recv_grad_vars(self, task, var_scope)
    # add loss with needed grad send op and get grad losses
    send_recv_grad_list = self.require_grad_ops[task]
    send_ops = [i[0] for i in send_recv_grad_list]
    recv_grad_ops = [i[1] for i in send_recv_grad_list]
    opt_config = kwargs.pop('opt_config', _DEFAULT_OPT_CONFIG)

    if loss in send_ops:
      loss = send_ops
      grad_loss = recv_grad_ops
    else:
      if 'opt_config' not in opt.compute_gradients.__code__.co_varnames:
        loss = self._reduce_loss(loss, opt_config.pop('REDUCE', 'mean'))
      loss = [loss] + send_ops
      grad_loss = [None] + recv_grad_ops

    if len(recv_grads) > 0:
      if opt_config.pop('BACKEND_MODE', 'noise') == 'noise':
        grads_lists = []
        for y, g in zip(loss, grad_loss):
          if y is None:
            continue
          if 'opt_config' not in opt.compute_gradients.__code__.co_varnames:
            grads = opt.compute_gradients(y, [v for _, v in recv_grads], grad_loss=g)
          else:
            grads = opt.compute_gradients(y, [v for _, v in recv_grads], grad_loss=g,
                                          opt_config=opt_config)
          grads = [g for g, _ in grads]
          grads_lists.append(grads)

        send_grads = None
        for l in grads_lists:
          if send_grads is None:
            send_grads = l
          else:
            send_grads = tf.nest.map_structure(tf.add, send_grads, l)
      else:
        send_grads = gradients_impl.gradients(loss, [v for _, v in recv_grads], grad_ys=grad_loss)

      for (name, v), grad in zip(recv_grads, send_grads):
        if grad is not None:
          self.send(name + '_grad', grad, task=task)
        else:
          self.send(name + '_grad', tf.zeros_like(v), task=task)


    def _safe_slice_add(a, b):
      if a is None:
        return b
      if b is None:
        return a
      return slice_op.slice_add(a, b)


    try:
      grads_lists = []
      for y, g in zip(loss, grad_loss):
        if y is None:
          continue
        if 'opt_config' not in opt.compute_gradients.__code__.co_varnames:
          grads = opt.compute_gradients(y, var_list, grad_loss=g)
        else:
          grads = opt.compute_gradients(y, var_list, grad_loss=g,
                                        opt_config=opt_config)
        grads = [g for g, _ in grads]
        grads_lists.append(grads)

      grads_list = None
      for l in grads_lists:
        if grads_list is None:
          grads_list = l
        else:
          grads_list = tf.nest.map_structure(_safe_slice_add, grads_list, l)

      grads_and_vars = list(zip(grads_list, var_list))
      return  opt.apply_gradients(grads_and_vars)
    except ValueError as e:
      tf.logging.warn(e + ' This warning may be caused by either legal or illegal action. Please confirm.')
      return

  def fit(self,
          procedure_fn,
          log_step = 100,
          project_name = "default_prj",
          **kwargs):
    session_patch.patch()
    self._add_logger_hook(log_step)
    self._create_stage_mgr(project_name)
    self._internal_fit(procedure_fn,
                       **kwargs)
