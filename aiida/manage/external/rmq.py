# -*- coding: utf-8 -*-
###########################################################################
# Copyright (c), The AiiDA team. All rights reserved.                     #
# This file is part of the AiiDA code.                                    #
#                                                                         #
# The code is hosted on GitHub at https://github.com/aiidateam/aiida-core #
# For further information on the license, see the LICENSE.txt file        #
# For further information please visit http://www.aiida.net               #
###########################################################################
# pylint: disable=cyclic-import
"""Components to communicate tasks to RabbitMQ."""
from __future__ import division
from __future__ import print_function
from __future__ import absolute_import
import collections
import logging

from tornado import gen
import plumpy
from kiwipy import communications, Future

__all__ = ('RemoteException', 'CommunicationTimeout', 'DeliveryFailed', 'ProcessLauncher')

LOGGER = logging.getLogger(__name__)

RemoteException = plumpy.RemoteException
DeliveryFailed = plumpy.DeliveryFailed
CommunicationTimeout = communications.TimeoutError  # pylint: disable=invalid-name

# GP: Using here 127.0.0.1 instead of localhost because on some computers
# localhost resolves first to IPv6 with address ::1 and if RMQ is not
# running on IPv6 one gets an annoying warning. When moving this to
# a user-configurable variable, make sure users are aware of this and
# know how to avoid warnings. For more info see
# https://github.com/aiidateam/aiida-core/issues/1142
_RMQ_URL = 'amqp://127.0.0.1'
_RMQ_TASK_PREFETCH_COUNT = 100  # This value should become configurable per profile at some point
_RMQ_HEARTBEAT_TIMEOUT = 600  # Maximum that can be set by client, with default RabbitMQ server configuration
_LAUNCH_QUEUE = 'process.queue'
_MESSAGE_EXCHANGE = 'messages'
_TASK_EXCHANGE = 'tasks'


def get_rmq_url(heartbeat_timeout=None):
    """
    Get the URL to connect to RabbitMQ

    :param heartbeat_timeout: the interval in seconds for the heartbeat timeout
    :returns: the connection URL string
    """
    url = _RMQ_URL

    if heartbeat_timeout is None:
        heartbeat_timeout = _RMQ_HEARTBEAT_TIMEOUT

    if heartbeat_timeout is not None:
        url += '?heartbeat={}'.format(heartbeat_timeout)

    return url


def get_rmq_config(prefix):
    """
    Get the RabbitMQ configuration dictionary for a given prefix. If the prefix is not
    specified, the prefix will be retrieved from the currently loaded profile configuration

    :param prefix: a string prefix for the RabbitMQ communication queues and exchanges
    :returns: the configuration dictionary for the RabbitMQ communicators
    """
    rmq_config = {'url': get_rmq_url(), 'prefix': prefix, 'task_prefetch_count': _RMQ_TASK_PREFETCH_COUNT}
    return rmq_config


def get_launch_queue_name(prefix=None):
    """
    Return the launch queue name with an optional prefix

    :returns: launch queue name
    """
    if prefix is not None:
        return '{}.{}'.format(prefix, _LAUNCH_QUEUE)

    return _LAUNCH_QUEUE


def get_message_exchange_name(prefix):
    """
    Return the message exchange name for a given prefix

    :returns: message exchange name
    """
    return '{}.{}'.format(prefix, _MESSAGE_EXCHANGE)


def get_task_exchange_name(prefix):
    """
    Return the task exchange name for a given prefix

    :returns: task exchange name
    """
    return '{}.{}'.format(prefix, _TASK_EXCHANGE)


def _store_inputs(inputs):
    """
    Try to store the values in the input dictionary. For nested dictionaries, the values are stored by recursively.
    """
    for node in inputs.values():
        try:
            node.store()
        except AttributeError:
            if isinstance(node, collections.Mapping):
                _store_inputs(node)


class ProcessLauncher(plumpy.ProcessLauncher):
    # pylint: disable=too-few-public-methods
    """
    A sub class of plumpy.ProcessLauncher to launch a Process

    It overrides the _continue method to make sure the node corresponding to the task can be loaded and
    that if it is already marked as terminated, it is not continued but the future is reconstructed and returned
    """

    @gen.coroutine
    def _continue(self, communicator, pid, nowait, tag=None):
        """
        Continue the task

        Note that the task may already have been completed, as indicated from the corresponding the node, in which
        case it is not continued, but the corresponding future is reconstructed and returned. This scenario may
        occur when the Process was already completed by another worker that however failed to send the acknowledgment.

        :param communicator: the communicator that called this method
        :param pid: the pid of the process to continue
        :param nowait: if True don't wait for the process to finish, just return the pid, otherwise wait and
            return the results
        :param tag: the tag of the checkpoint to continue from
        """
        from aiida.common import exceptions
        from aiida.engine import ProcessState
        from aiida.engine.exceptions import PastException
        from aiida.orm import load_node, Data
        from aiida.orm.utils import serialize

        try:
            node = load_node(pk=pid)
        except (exceptions.MultipleObjectsError, exceptions.NotExistent):
            # In this case, the process node corresponding to the process id, cannot be resolved uniquely or does not
            # exist. The latter being the most common case, where someone deleted the node, before the process was
            # properly terminated. Since the node is never coming back and so the process will never be able to continue
            # we raise `Return` instead of `TaskRejected` because the latter would cause the task to be resent and start
            # to ping-pong between RabbitMQ and the daemon workers.
            LOGGER.exception('Cannot continue process<%d>', pid)
            raise gen.Return(False)

        if node.is_terminated:

            LOGGER.info('not continuing process<%d> which is already terminated with state %s', pid, node.process_state)

            future = Future()

            if node.is_finished:
                future.set_result({entry.link_label: entry.node for entry in node.get_outgoing(node_class=Data)})
            elif node.is_excepted:
                future.set_exception(PastException(node.exception))
            elif node.is_killed:
                future.set_exception(plumpy.KilledError())

            raise gen.Return(future.result())

        try:
            result = yield super(ProcessLauncher, self)._continue(communicator, pid, nowait, tag)
        except Exception as exception:
            # If the process state of the node has not yet been put to excepted, the exception was raised before the
            # process instance could be reconstructed, for example when the process class could not be loaded, thereby
            # circumventing the exception handling of the state machine. Raising this exception will then acknowledge
            # the process task with RabbitMQ leaving an uncleaned node in the `CREATED` state for ever. Therefore we
            # have to perform the node cleaning manually.
            if not node.is_excepted:
                node.logger.exception('failed to recreate the process instance in order to continue it')
                node.set_exception(str(exception))
                node.set_process_state(ProcessState.EXCEPTED)
                node.seal()
            raise

        # Ensure that the result is serialized such that communication thread won't have to do database operations
        try:
            serialized = serialize.serialize(result)
        except Exception:
            LOGGER.exception('failed to serialize the result for process<%d>', pid)
            raise

        raise gen.Return(serialized)
