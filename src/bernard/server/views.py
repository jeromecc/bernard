# coding: utf-8
from functools import (
    wraps,
)

from aiohttp.web_request import (
    Request,
)
from aiohttp.web_response import (
    Response,
    json_response,
)

from bernard.analytics.base import (
    providers,
)
from bernard.conf import (
    settings,
)
from bernard.engine.platform import (
    Platform,
)
from bernard.engine.request import (
    BaseMessage,
)
from bernard.layers import (
    Postback,
)
from bernard.middleware import (
    MiddlewareManager,
)
from bernard.platforms import (
    manager,
)


def bernard_auth(func):
    """
    Authenticates the users based on the query-string-provided token
    """

    @wraps(func)
    async def wrapper(request: Request):
        hdr_key = settings.WEBVIEW_AUTH_HEADER
        hdr_token = request.headers.get(hdr_key)

        try:
            body = await request.json()
        except ValueError:
            body = None

        msg, platform = await manager.message_from_token(hdr_token, body)

        if not msg:
            return json_response({
                'status': 'unauthorized',
                'message': 'No valid token found in GET parameter '
                           f'"{hdr_key}"',
            }, status=401)

        return await func(msg, platform)
    return wrapper


@bernard_auth
async def postback_me(msg: BaseMessage, platform: Platform) -> Response:
    """
    Provides the front-end with details about the user. This output can be
    completed using the `api_postback_me` middleware hook.
    """

    async def get_basic_info():
        user = msg.get_user()

        return {
            'friendly_name': await user.get_friendly_name(),
            'locale': await user.get_locale(),
        }

    func = MiddlewareManager.instance().get('api_postback_me', get_basic_info)

    return json_response(await func())


@bernard_auth
async def postback_send(msg: BaseMessage, platform: Platform) -> Response:
    """
    Injects the POST body into the FSM as a Postback message.
    """

    await platform.inject_message(msg)

    return json_response({
        'status': 'ok',
    })


@bernard_auth
async def postback_analytics(msg: BaseMessage, platform: Platform) -> Response:
    """
    Makes a call to an analytics function.
    """

    try:
        pb = msg.get_layers()[0]
        assert isinstance(pb, Postback)

        user = msg.get_user()
        user_lang = await user.get_locale()
        user_id = user.id

        if pb.payload['event'] == 'page_view':
            func = 'page_view'
            path = pb.payload['path']
            title = pb.payload.get('title', '')
            args = [path, title, user_id, user_lang]
        else:
            return json_response({
                'status': 'unknown event',
                'message': f'"{pb.payload["event"]}" is not a recognized '
                           f'analytics event',
            })

        async for p in providers():
            getattr(p, func)(*args)

    except (KeyError, IndexError, AssertionError, TypeError):
        return json_response({
            'status': 'missing data'
        }, status=400)

    else:
        return json_response({
            'status': 'ok',
        })


async def health_check(request: Request) -> Response:
    """
    A simple non-authenticated endpoint to check the health of the process.
    """

    return json_response({
        'status': 'ok',
    })