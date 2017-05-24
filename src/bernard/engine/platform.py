# coding: utf-8
from typing import Callable, List
from bernard.engine.responder import Responder
from bernard.layers import Stack
from .request import BaseMessage


MessageCallback = Callable[[BaseMessage, Responder, bool], None]


class Platform(object):
    """
    Base class for the platforms. You need to overload it in order to create
    your own platform.

    To create a new platform, you need two things:

        - Call `_notify()` when you receive a message from the platform
        - Implement `accept()`

    The `fsm_creates_task` attribute indicates one of two work modes:

        - True: the callbacks are in charge of starting their own tasks
        - False: the callbacks will be awaited. Also, the return value of the
          last callback will be stored in `_register`. This mode is created
          for unit tests.
    """

    fsm_creates_task = True

    def __init__(self):
        self._listeners = []  # type: List[MessageCallback]
        self._register = None

    def on_message(self, cb: MessageCallback):
        """
        Register a callback to listen for incoming messages.
        """

        self._listeners.append(cb)

    async def _notify(self, message: BaseMessage, responder: Responder):
        """
        Notify all callbacks that a message was received.
        """
        for cb in self._listeners:
            coro = cb(message, responder, self.fsm_creates_task)

            if not self.fsm_creates_task:
                self._register = await coro

    def accept(self, stack: Stack):
        """
        Return True if the platform can accept the stack provided as argument,
        and False otherwise.
        """
        raise NotImplementedError