import asyncio
import functools
import itertools
import json
import logging

import voluptuous
import websockets

import serverstf.cache


log = logging.getLogger(__name__)


class WebsocketError(Exception):
    """Base exception for all websocket related errors."""


class MessageError(WebsocketError):
    """Raised for message validation failures."""


def validate(schema):
    """Validate message entities against a schema.

    This is a decorator to help message handlers validate the entity against
    a :mod:`voluptuous` schema. The decorated function will raise
    :exc:`MessageError` if the entity is not valid according to the given
    schema. If the entity is valid then the wrapped function will be called
    being passed the validated entity as the sole argument.

    :param schema: a :mod:`voluptuous` schema specification.
    """

    def decorator(function):
        if not asyncio.iscoroutinefunction(function):
            raise TypeError(
                "{!r} is not a coroutine function".format(function))

        @asyncio.coroutine
        @functools.wraps(function)
        def wrapper(self, entity):
            try:
                validated_entity = voluptuous.Schema(schema)(entity)
            except voluptuous.Invalid as exc:
                raise MessageError("Entity: {}".format(exc)) from exc
            yield from function(self, validated_entity)

        return wrapper

    return decorator


def address(value):
    """Convert a dictionary to a :class:`serverstf.cache.Address`.

    The dictionary must have an ``ip`` and ``port`` field which are a
    string and integer respectively.
    """
    return serverstf.cache.Address(*voluptuous.Schema({
        voluptuous.Required("ip"): str,
        voluptuous.Required("port"): int,
    })(value).values())


class Client:
    """Encapsulates a single websocket connection.

    Instances of this class handle communication to and from a connected
    client. It handles requests to subscribe to server status updates and
    then publishes to the connected peer.

    Websockets are message oriented. On the wire each message is a UTF-8
    string but this class only deals with Unicode. The messages themself
    are JSON objects. These objects are referred to as message envelopes.
    Each envelope has two fields:

    ``type``
        This is a string that identifies the type of message contained
        within the envelope.

    ``entity``
        This is the *body* of the message. Its structure is dependant on
        the message type but any JSON value is acceptable in this field.

    Communication between the server (that's us) and the client is very much
    fire and forget. It is not possible to have a request-reply model due
    to the fact that status updates may happen at any time.
    """

    def __init__(self, websocket, cache):
        self._websocket = websocket
        self._cache = cache
        self._subscriptions = []
        self._send_queue = asyncio.Queue()

    @asyncio.coroutine
    def send(self, type_, entity):
        """Enqueue a message to be sent to peer."""
        message = {"type": str(type_), "entity": entity}
        yield from self._send_queue.put(json.dumps(message))

    @validate(address)
    @asyncio.coroutine
    def _handle_subscribe(self, address):
        log.info("New subscription to address %s", address)
        # TODO: everything

    @asyncio.coroutine
    def _dispatch(self, raw_message):
        """Handle a JSON encoded message.

        This will decode the message as JSON and validate the envelope to
        ensure it has the necessary fields. If the envelope is valid then
        a method is looked up that is capable of dealing with the given
        message type.

        Message type handler methods should be named with the message type
        prefixed by ``_handle_``. For example, the handler for messages of
        type ``foo`` would be handled by :meth:`_handle_foo`.

        If a handler method exists for the message type then it will be
        called with the message entity passed in as the sole argument.

        Method handlers must be coroutine functions.

        :raises MessageError: if the message isn't JSON, the envelope
            is invalid (e.g. missing fields or wrong type) or there is no
            handler method for the given message type.
        """
        try:
            message = json.loads(raw_message)
        except ValueError as exc:
            raise MessageError("JSON: {}".format(exc)) from exc
        try:
            voluptuous.Schema({
                voluptuous.Required("type"): str,
                voluptuous.Required("entity"): lambda x: x,
            })(message)
        except voluptuous.Invalid as exc:
            raise MessageError("Envelope: {}".format(exc)) from exc
        handler = getattr(self, "_handle_" + message["type"], None)
        if not handler or not asyncio.iscoroutinefunction(handler):
            raise MessageError(
                "Unknown message type: {}".format(message["type"]))
        yield from handler(message["entity"])

    @asyncio.coroutine
    def _read(self):
        """Continually receive and handle incoming messages.

        This will continually attempt to receive messages from the websocket
        and dispatch them to appropriate handlers. When malformed messages
        are received or there is an unexpected error then the client will
        be notified.

        If the client disconnects then the coroutine will return.
        """
        while True:
            received = yield from self._websocket.recv()
            if received is None:
                return
            try:
                yield from self._dispatch(received)
            except MessageError as exc:
                log.warning("Received bad message: %s", exc)
                yield from self.send("error", str(exc))
            except Exception as exc:
                log.exception(
                    "Error handling %r for %s",  received, self._websocket)
                # Unhandled exception. To be safe we shouldn't blindly send
                # this error unabridged to the client.
                # TODO: send notification of internal error

    @asyncio.coroutine
    def _write(self):
        """Continually flush the send queue."""
        while True:
            message = yield from self._send_queue.get()
            yield from self._websocket.send(message)

    @asyncio.coroutine
    def process(self):
        """Process websocket communication.

        This starts a number of concurrent tasks which are used to read
        incoming messages and continually flush the send queue. These tasks
        will run until one of them exits (either due to an error or the
        peer disconnecting), at which point all outstanding tasks are
        cancelled and this coroutine returns.
        """
        log.debug("Handling new socket %s", self._websocket)
        done, pending = yield from asyncio.wait([
            self._read(),
            self._write(),
        ], return_when=asyncio.FIRST_COMPLETED)
        log.debug("Socket handler for %s finished", self._websocket)
        for task in itertools.chain(done, pending):
            task.cancel()
            try:
                task.result()
            except asyncio.InvalidStateError:
                # The task hasn't had chance to cancel yet but that doesn't
                # really matter.
                pass
            except Exception:
                log.exception("Error handling %s "
                              "in task %s", self._websocket, task)


class Service:

    #: The path the service is served from
    PATH = "/"

    def __init__(self, cache):
        self._cache = cache

    @asyncio.coroutine
    def __call__(self, websocket, path):
        if path != self.PATH:
            log.error("Client connected on path %s; dropping connection", path)
            return
        client = Client(websocket, self._cache)
        yield from client.process()
        log.debug("Connection closed")


def _websocket_args(parser):
    parser.add_argument(
        'port',
        type=int,
        help="The port the websocket service will listen on.",
    )
    parser.add_argument(
        "url",
        type=serverstf.redis_url,
        nargs="?",
        default="//localhost",
        help="The URL of the Redis database to use for the cache and queues."
    )


@asyncio.coroutine
def _websocket_async_main(args, loop):
    log.info("Starting websocket server on port %i", args.port)
    cache_context = \
        yield from serverstf.cache.AsyncCache.connect(args.url, loop)
    with cache_context as cache:
        yield from websockets.serve(Service(cache), port=args.port)
    log.info("Stopping websocket server")


@serverstf.subcommand("websocket", _websocket_args)
def _websocket_main(args):
    loop = asyncio.get_event_loop()
    loop.run_until_complete(_websocket_async_main(args, loop))
    loop.run_forever()
