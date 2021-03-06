r"""
This module implements the core RPC and publish APIs. Developers can extend
LinkProtocol to provide additional RPC callbacks for their web-applications. Then extend
ServerProtocol to hook all the needed LinkProtocols together.
"""

from __future__ import absolute_import, division, print_function

import inspect, logging, json, sys, traceback, re

from twisted.web            import resource
from twisted.python         import log
from twisted.internet       import reactor
from twisted.internet       import defer
from twisted.internet.defer import Deferred, returnValue

from . import register as exportRpc
from autobahn.twisted.websocket import WebSocketServerFactory
from autobahn.twisted.websocket import WebSocketServerProtocol

try:
    basestring
except NameError:
    basestring = str


# =============================================================================
#
# Base class for objects that can accept RPC calls or publish over wslink
#
# =============================================================================

class LinkProtocol(object):
    """
    Subclass this to communicate with wslink clients. LinkProtocol
    objects provide rpc and pub/sub actions.
    """
    def __init__(self):
        # need a no-op in case they are called before connect.
        self.publish = lambda x, y: None
        self.addAttachment = lambda x: None
        self.coreServer = None

    def init(self, publish, addAttachment):
        self.publish = publish
        self.addAttachment = addAttachment

    def getSharedObject(self, key):
        if self.coreServer:
            return self.coreServer.getSharedObject(key)
        return None

# =============================================================================
#
# Base class for wslink ServerProtocol objects
#
# =============================================================================

class ServerProtocol(object):
    """
    Defines the core server protocol for wslink. Gathers a list of LinkProtocol
    objects that provide rpc and publish functionality.
    """

    def __init__(self):
        self.linkProtocols = []
        self.secret = None
        self.initialize()

    def init(self, publish, addAttachment):
        self.publish = publish
        self.addAttachment = addAttachment

    def initialize(self):
        """
        Let sub classes define what they need to do to properly initialize
        themselves.
        """
        pass

    def setSharedObject(self, key, shared):
        if not hasattr(self, "sharedObjects"):
            self.sharedObjects = {}
        if (shared == None and key in self.sharedObjects):
            del self.sharedObjects[key]
        else:
            self.sharedObjects[key] = shared

    def getSharedObject(self, key):
        if (key in self.sharedObjects):
            return self.sharedObjects[key]
        else:
            return None

    def registerLinkProtocol(self, protocol):
        assert( isinstance(protocol, LinkProtocol))
        protocol.coreServer = self
        self.linkProtocols.append(protocol)

    # Note: this can only be used _before_ a connection is made -
    # otherwise the WslinkWebSocketServerProtocol will already have stored references to
    # the RPC methods in the protocol.
    def unregisterLinkProtocol(self, protocol):
        assert( isinstance(protocol, LinkProtocol))
        protocol.coreServer = None
        try:
            self.linkProtocols.remove(protocol)
        except ValueError as e:
            log.error("Link protocol missing from registered list.")


    def getLinkProtocols(self):
        return self.linkProtocols

    def updateSecret(self, newSecret):
        self.secret = newSecret

    @exportRpc("application.exit")
    def exit(self):
        """RPC callback to exit"""
        reactor.stop()

    @exportRpc("application.exit.later")
    def exitLater(self, secondsLater=60):
        """RPC callback to exit after a short delay"""
        reactor.callLater(secondsLater, reactor.stop)

# =============================================================================
#
# Base class for wslink WebSocketServerFactory
#
# =============================================================================

class TimeoutWebSocketServerFactory(WebSocketServerFactory):
    """
    TimeoutWebSocketServerFactory is WebSocketServerFactory subclass
    that adds support to close the web-server after a timeout when the last
    connected client drops.

    The protocol must call connectionMade() and connectionLost() methods
    to notify this object that the connection was started/closed.
    If the connection count drops to zero, then the reap timer
    is started which will end the process if no other connections are made in
    the timeout interval.
    """

    def __init__(self, *args, **kwargs):
        self._connection_count = 0
        self.clientCount = 0
        self._timeout = kwargs['timeout']
        # _timeout of 0 indicates no timeout.
        if self._timeout > 0:
            self._reaper = reactor.callLater(self._timeout, lambda: reactor.stop())
        else:
            self._reaper = None
        self._protocolHandler = None

        del kwargs['timeout']
        WebSocketServerFactory.__init__(self, *args, **kwargs)
        WebSocketServerFactory.protocol = TimeoutWebSocketServerProtocol

    def connectionMade(self):
        if self._reaper:
            log.msg("Client has reconnected, cancelling reaper", logLevel=logging.DEBUG)
            self._reaper.cancel()
            self._reaper = None
        self._connection_count += 1
        self.clientCount += 1
        log.msg("on_connect: connection count = %s" % self._connection_count, logLevel=logging.DEBUG)

    def connectionLost(self, reason):
        if self._connection_count > 0:
            self._connection_count -= 1
        log.msg("connection_lost: connection count = %s" % self._connection_count, logLevel=logging.DEBUG)

        if self._connection_count == 0 and not self._reaper and self._timeout > 0:
            log.msg("Starting timer, process will terminate in: %ssec" % self._timeout, logLevel=logging.DEBUG)
            self._reaper = reactor.callLater(self._timeout, lambda: reactor.stop())

    def setServerProtocol(self, newServerProtocol):
        self._protocolHandler = newServerProtocol

    def getServerProtocol(self):
        return self._protocolHandler

    def getClientCount(self):
        return self.clientCount

# =============================================================================

class TimeoutWebSocketServerProtocol(WebSocketServerProtocol, object):

    def connectionMade(self):
        WebSocketServerProtocol.connectionMade(self)
        # print(self.factory)
        self.factory.connectionMade()

    def connectionLost(self, reason):
        WebSocketServerProtocol.connectionLost(self, reason)
        self.factory.connectionLost(reason)


# =============================================================================
# singleton publish manager

class PublishManager(object):
    def __init__(self):
        self.protocols = []
        self.attachmentMap = {}
        self.attachmentId = 0
        self.publishCount = 0

    def registerProtocol(self, protocol):
        self.protocols.append(protocol)

    def unregisterProtocol(self, protocol):
        if protocol in self.protocols:
            self.protocols.remove(protocol)

    def getAttachmentMap(self):
        return self.attachmentMap

    def clearAttachmentMap(self):
        self.attachmentMap.clear()

    def addAttachment(self, payload):
        # print("attachment", self, self.attachmentId)
        # use a string flag in place of the binary attachment.
        binaryId = 'wslink_bin{0}'.format(self.attachmentId)
        self.attachmentMap[binaryId] = payload
        self.attachmentId += 1
        return binaryId

    def publish(self, topic, data):
        for protocol in self.protocols:
            # The client is unknown - we send to any client who is subscribed to the topic
            rpcid = 'publish:{0}:{1}'.format(topic, self.publishCount)
            protocol.sendWrappedMessage(rpcid, data)
        self.clearAttachmentMap()

# singleton, used by all instances of WslinkWebSocketServerProtocol
publishManager = PublishManager()

# from http://www.jsonrpc.org/specification, section 5.1
METHOD_NOT_FOUND = -32601
AUTHENTICATION_ERROR = -32000
EXCEPTION_ERROR = -32001
RESULT_SERIALIZE_ERROR = -32002
# used in client JS code:
CLIENT_ERROR = -32099

# -----------------------------------------------------------------------------
# WS protocol definition
# -----------------------------------------------------------------------------

class WslinkWebSocketServerProtocol(TimeoutWebSocketServerProtocol):
    def __init__(self):
        super(WslinkWebSocketServerProtocol, self).__init__()
        self.functionMap = {}
        self.attachmentsReceived = {}
        self.attachmentsRecvQueue = []

    def onConnect(self, request):
        self.clientID = self.factory.getClientCount()
        log.msg("client connected, id: {}".format(self.clientID), logLevel=logging.INFO)   # request)
        # Build the rpc method dictionary. self.factory isn't set until connected.
        if not self.factory.getServerProtocol():
            return
        protocolList = self.factory.getServerProtocol().getLinkProtocols()
        protocolList.append(self.factory.getServerProtocol())
        for protocolObject in protocolList:
            protocolObject.init(self.publish, self.addAttachment)
            test = lambda x: inspect.ismethod(x) or inspect.isfunction(x)
            for k in inspect.getmembers(protocolObject.__class__, test):
                proc = k[1]
                if "_wslinkuris" in proc.__dict__:
                    uri_info = proc.__dict__["_wslinkuris"][0]
                    if "uri" in uri_info:
                        uri = uri_info["uri"]
                        self.functionMap[uri] = (protocolObject, proc)
        publishManager.registerProtocol(self)

    def onClose(self, wasClean, code, reason):
        log.msg("client closed, clean: {}, code: {}, reason: {}".format(wasClean, code, reason), logLevel=logging.INFO)
        publishManager.unregisterProtocol(self)

    def handleSystemMessage(self, rpcid, methodName, args):
        rpcList = rpcid.split(":")
        if rpcList[0] == "system":
            if (methodName == "wslink.hello"):
                if (args and args[0] and (type(args[0]) is dict) and ("secret" in args[0]) \
                    and (args[0]["secret"] == self.factory.getServerProtocol().secret)):
                    self.sendWrappedMessage(rpcid, { "clientID": "c{0}".format(self.clientID) })
                else:
                    self.sendWrappedError(rpcid, AUTHENTICATION_ERROR, "Authentication failed")
            else:
                self.sendWrappedError(rpcid, METHOD_NOT_FOUND, "Unknown system method called")
            return True
        return False


    def onMessage(self, payload, isBinary):
        if isBinary:
            # assume all binary messages are attachments
            try:
                key = self.attachmentsRecvQueue.pop(0)
                self.attachmentsReceived[key] = payload
            except:
                pass
            return

        # handles issue https://bugs.python.org/issue10976
        # `payload` is type bytes in Python 3. Unfortunately, json.loads
        # doesn't support taking bytes until Python 3.6.
        if type(payload) is bytes:
            payload = payload.decode('utf-8')

        rpc = json.loads(payload)
        log.msg("wslink incoming msg %s" % payload, logLevel=logging.DEBUG)
        if 'id' not in rpc:
            # should be a binary attachment header
            if rpc.get('method') == 'wslink.binary.attachment':
                keys = rpc.get('args', [])
                if isinstance(keys, list):
                    for k in keys:
                        # wait for an attachment by it's order
                        self.attachmentsRecvQueue.append(k)
            return

        # TODO validate
        version = rpc['wslink']
        rpcid = rpc['id']
        methodName = rpc['method']

        args = []
        kwargs = {}
        if ('args' in rpc) and isinstance(rpc['args'], list):
            args = rpc['args']
        if ('kwargs' in rpc) and isinstance(rpc['kwargs'], dict):
            kwargs = rpc['kwargs']

        # Check for system messages, like hello
        if (self.handleSystemMessage(rpcid, methodName, args)):
            return

        if (not methodName in self.functionMap):
            self.sendWrappedError(rpcid, METHOD_NOT_FOUND, "Unregistered method called", methodName)
            return

        obj,func = self.functionMap[methodName]
        try:
            # get any attachments
            def findAttachments(o):
                if isinstance(o, basestring) and \
                        re.match(r'^wslink_bin\d+$', o) and \
                        o in self.attachmentsReceived:
                    attachment = self.attachmentsReceived[o]
                    del self.attachmentsReceived[o]
                    return attachment
                elif isinstance(o, list):
                    for i, v in enumerate(o):
                        o[i] = findAttachments(v)
                elif isinstance(o, dict):
                    for k in o:
                        o[k] = findAttachments(o[k])
                return o
            args = findAttachments(args)
            kwargs = findAttachments(kwargs)

            results = func(obj, *args, **kwargs)
        except Exception as e:
            self.sendWrappedError(rpcid, EXCEPTION_ERROR, "Exception raised",
                { "method": methodName, "exception": repr(e), "trace": traceback.format_exc() })
            return

        self.sendWrappedMessage(rpcid, results, method=methodName)
        # sent reply to RPC, no attachment re-use.
        publishManager.clearAttachmentMap()

    def sendWrappedMessage(self, rpcid, content, method=''):
        wrapper = {
            "wslink": "1.0",
            "id": rpcid,
            "result": content,
        }
        try:
            encMsg = json.dumps(wrapper, ensure_ascii = False).encode('utf8')
        except TypeError as e:
            # the content which is not serializable might be arbitrarily large, don't include.
            # repr(content) would do that...
            self.sendWrappedError(rpcid, RESULT_SERIALIZE_ERROR, "Method result cannot be serialized", method)
            return

        # Check if any attachments in the map go with this message
        attachments = publishManager.getAttachmentMap()
        if attachments:
            for key in attachments:
                # string match the encoded attachment key
                if key.encode('utf8') in encMsg:
                    # send header
                    header = {
                        "wslink": "1.0",
                        "method": "wslink.binary.attachment",
                        "args": [key],
                    }
                    self.sendMessage(json.dumps(header, ensure_ascii = False).encode('utf8'))
                    # Send binary message
                    self.sendMessage(attachments[key], True)

        self.sendMessage(encMsg)

    def sendWrappedError(self, rpcid, code, message, data = None):
        wrapper = {
            "wslink": "1.0",
            "id": rpcid,
            "error": {
                "code": code,
                "message": message,
            },
        }
        if (data):
            wrapper["error"]["data"] = data
        encMsg = json.dumps(wrapper, ensure_ascii = False).encode('utf8')
        self.sendMessage(encMsg)


    def publish(self, topic, data):
        publishManager.publish(topic, data)

    def addAttachment(self, payload):
        return publishManager.addAttachment(payload)

    def setSecret(self, newSecret):
        self.secret = newSecret
