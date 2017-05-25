# coding: utf-8
import aiohttp
import ujson
from textwrap import wrap
from typing import Text, Coroutine, List, Any, Dict
from bernard.engine.responder import UnacceptableStack, Responder
from bernard.engine.request import Request, BaseMessage, User, Conversation
from bernard.i18n.translator import render
from bernard.layers import Stack, BaseLayer
from bernard import layers as lyr
from bernard.engine.platform import Platform, PlatformOperationError
from bernard.conf import settings

MESSAGES_ENDPOINT = 'https://graph.facebook.com/v2.6/me/messages'


class FacebookUser(User):
    """
    That is the Facebook user class. So far it just computes the unique user
    ID.
    """

    def __init__(self, fbid: Text):
        self.fbid = fbid
        super(FacebookUser, self).__init__(self._fbid_to_id(fbid))

    def _fbid_to_id(self, fbid: Text):
        """
        Transforms a Facebook user ID into a unique user ID.
        """
        return 'facebook:user:{}'.format(fbid)


class FacebookConversation(Conversation):
    """
    That is a Facebook conversation. Some idea as the user.
    """

    def __init__(self, fbid: Text):
        self.fbid = fbid
        super(FacebookConversation, self).__init__(self._fbid_to_id(fbid))

    def _fbid_to_id(self, fbid: Text):
        """
        Facebook ID into conversation ID. So far we just handle user-to-bot
        cases, but who knows it might change in the future.
        """
        return 'facebook:conversation:user:{}'.format(fbid)


class FacebookMessage(BaseMessage):
    """
    Decodes the raw JSON sent by Facebook and allow to extract the user and the
    accompanying layers.
    """

    def __init__(self, event):
        self._event = event

    def get_platform(self) -> Text:
        """
        The platform is always Facebook
        """
        return 'facebook'

    def get_user(self) -> FacebookUser:
        """
        Generate a Facebook user instance
        """
        return FacebookUser(self._event['sender']['id'])

    def get_conversation(self) -> FacebookConversation:
        """
        Generate a Facebook conversation instance
        """
        return FacebookConversation(self._event['sender']['id'])

    def get_layers(self) -> List[BaseLayer]:
        """
        Return all layers that can be found in the message.
        """
        out = []
        msg = self._event.get('message', {})

        if 'text' in msg:
            out.append(lyr.RawText(msg['text']))

        if 'quick_reply' in msg:
            out.append(lyr.QuickReply(msg['quick_reply']['payload']))

        return out

    def get_page_id(self) -> Text:
        """
        That's for internal use, extract the Facebook page ID.
        """
        return self._event['recipient']['id']


class FacebookResponder(Responder):
    """
    Not much to do here
    """


class Facebook(Platform):
    PATTERNS = {
        'text': '(Text|RawText)+ QuickRepliesList?',
    }

    def __init__(self):
        super(Facebook, self).__init__()
        self.session = None

    async def async_init(self):
        """
        During async init we just need to create a HTTP session so we can keep
        outgoing connexions to FB alive.
        """
        self.session = aiohttp.ClientSession()

    def accept(self, stack: Stack):
        """
        Checks that the stack can be accepted according to the `PATTERNS`.

        If the pattern is found, then its name is stored in the `annotation`
        attribute of the stack.
        """

        for name, pattern in self.PATTERNS.items():
            if stack.match_exp(pattern):
                stack.annotation = name
                return True
        return False

    def send(self, request: Request, stack: Stack) -> Coroutine:
        """
        Send a stack to Facebook

        Actually this will delegate to one of the `_send_*` functions depending
        on what the stack looks like.
        """

        if stack.annotation not in self.PATTERNS:
            if not self.accept(stack):
                raise UnacceptableStack('Cannot accept stack {}'.format(stack))

        func = getattr(self, '_send_' + stack.annotation)
        return func(request, stack)

    async def handle_event(self, event: FacebookMessage):
        """
        Handle an incoming message from Facebook.
        """
        responder = FacebookResponder(self)
        await self._notify(event, responder)

    def _access_token(self, request: Request):
        """
        Guess the access token for that specific request.
        """

        msg = request.message  # type: FacebookMessage
        page_id = msg.get_page_id()

        for page in settings.FACEBOOK:
            if page['page_id'] == page_id:
                return page['page_token']

        raise PlatformOperationError('Trying to get access token of the '
                                     'page "{}", which is not configured.'
                                     .format(page_id))

    def _make_qr(self, qr: lyr.QuickRepliesList.BaseOption, request: Request):
        """
        Generate a single quick reply's content.
        """

        if isinstance(qr, lyr.QuickRepliesList.TextOption):
            return {
                'content_type': 'text',
                'title': render(qr.text, request),
                'payload': qr.slug,
            }
        elif isinstance(qr, lyr.QuickRepliesList.LocationOption):
            return {
                'content_type': 'location',
            }

    async def _send_text(self, request: Request, stack: Stack):
        """
        Send text layers to the user. Each layer will go in its own bubble.
        
        Also, Facebook limits messages to 320 chars, so if any message is
        longer than that it will be split into as many messages as needed to
        be accepted by Facebook.
        """

        parts = []

        for layer in stack.layers:
            if isinstance(layer, (lyr.Text, lyr.RawText)):
                text = render(layer.text, request)
                for part in wrap(text, 320):
                    parts.append(part)

        for part in parts[:-1]:
            await self._send(request, {
                'text': part,
            })

        part = parts[-1]

        msg = {
            'text': part,
        }

        try:
            qr = stack.get_layer(lyr.QuickRepliesList)
        except KeyError:
            pass
        else:
            # noinspection PyUnresolvedReferences
            msg['quick_replies'] = [
                self._make_qr(o, request) for o in qr.options
            ]

        await self._send(request, msg)

    async def _handle_fb_response(self, response: aiohttp.ClientResponse):
        """
        Check that Facebook was OK with the API call we just made and raise
        an exception if it failed.
        """

        ok = response.status == 200

        if not ok:
            # noinspection PyBroadException
            try:
                error = (await response.json())['error']['message']
            except Exception:
                error = '(nothing)'

            raise PlatformOperationError('Facebook says: "{}"'
                                         .format(error))

    async def _send(self, request: Request, content: Dict[Text, Any]):
        """
        Actually proceed to sending the message to the Facebook API.
        """

        msg = {
            'recipient': {
                'id': request.conversation.fbid,
            },
            'message': content,
        }

        headers = {
            'content-type': 'application/json',
        }

        params = {
            'access_token': self._access_token(request),
        }

        post = self.session.post(
            MESSAGES_ENDPOINT,
            params=params,
            data=ujson.dumps(msg),
            headers=headers,
        )

        async with post as r:
            await self._handle_fb_response(r)
