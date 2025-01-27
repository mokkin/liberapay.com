import socket
from urllib.parse import urlsplit, urlunsplit

from pando import Response
from pando.http.request import Line
import pando.state_chain
from requests.exceptions import ConnectionError, Timeout

from .. import constants
from ..exceptions import LazyResponse, TooManyRequests
from ..security.csrf import SAFE_METHODS
from . import urlquote


def attach_environ_to_request(environ, request):
    request.country = request.headers.get(b'Cf-Ipcountry', b'').decode() or None
    request.environ = environ


def create_response_object(request, website):
    response = Response()
    response.request = request
    response.website = website
    return {'response': response}


def reject_requests_bypassing_proxy(request, response):
    """Reject requests that bypass Cloudflare, except health checks.
    """
    if request.bypasses_proxy and request.path.raw != '/callbacks/health':
        raise response.error(403, "The request bypassed a proxy.")


def canonize(request, website):
    """Enforce a certain scheme and hostname.

    This is a Pando state chain function to ensure that requests are served on a
    certain root URL, even if multiple domains point to the application.
    """
    try:
        request.hostname = host = request.headers[b'Host'].decode('idna')
    except UnicodeDecodeError:
        request.hostname = host = ''
    if request.path.raw.startswith('/callbacks/'):
        # Don't redirect callbacks
        if request.path.raw[-1] == '/':
            # Remove trailing slash
            l = request.line
            scheme, netloc, path, query, fragment = urlsplit(l.uri)
            assert path[-1] == '/'  # sanity check
            path = path[:-1]
            new_uri = urlunsplit((scheme, netloc, path, query, fragment))
            request.line = Line(l.method.raw, new_uri, l.version.raw)
        return
    canonical_host = website.canonical_host
    canonical_scheme = website.canonical_scheme
    scheme = request.headers.get(b'X-Forwarded-Proto', b'http')
    bad_scheme = scheme.decode('ascii', 'replace') != canonical_scheme
    bad_host = False
    if canonical_host:
        if host == canonical_host:
            pass
        elif host.endswith('.'+canonical_host):
            subdomain = host[:-len(canonical_host)-1]
            if subdomain in website.locales:
                accept_langs = request.headers.get(b'Accept-Language', b'')
                accept_langs = subdomain.encode('idna') + b',' + accept_langs
                request.headers[b'Accept-Language'] = accept_langs
            else:
                bad_host = True
        else:
            bad_host = True
    if bad_scheme or bad_host:
        url = '%s://%s' % (canonical_scheme, canonical_host if bad_host else host)
        if request.method in SAFE_METHODS:
            # Redirect to a particular path for idempotent methods.
            url += request.line.uri.path.decoded
            if request.line.uri.querystring:
                url += '?' + request.line.uri.querystring.decoded
        else:
            # For non-idempotent methods, redirect to homepage.
            url += '/'
        response = Response()
        response.headers[b'Cache-Control'] = b'public, max-age=86400'
        response.redirect(url)


def insert_constants():
    return {'constants': constants}


def enforce_rate_limits(request, user, website):
    if request.method in ('GET', 'HEAD'):
        return
    if user.id:
        website.db.hit_rate_limit('http-unsafe.user', user.id, TooManyRequests)
    else:
        website.db.hit_rate_limit('http-unsafe.ip-addr', request.source, TooManyRequests)


def set_output_to_None(state):
    # This is a temporary workaround for a shortcoming in Pando 0.47
    state.setdefault('output', None)


def add_content_disposition_header(request, response):
    """Tell the browser if the response is meant to be saved into a file.

    https://tools.ietf.org/html/rfc6266 and https://tools.ietf.org/html/rfc8187
    """
    save_as = request.qs.get('save_as')
    if save_as:
        save_as = urlquote(save_as, encoding='utf8').encode('ascii')
        response.headers[b'Content-Disposition'] = b"attachment; filename*=UTF-8''" + save_as


def merge_exception_into_response(state, exception, response=None):
    if response is None or not isinstance(exception, Response):
        return
    # clear the exception
    state['exception'] = None
    # set debug info
    exception.set_whence_raised()
    # render response if it's lazy
    if isinstance(exception, LazyResponse):
        exception.render_body(state)
        exception.__dict__.pop('lazy_body', None)
    # there's nothing else to do if the exception is the response
    if exception is response:
        return
    # merge cookies
    response.headers.cookie.update(exception.headers.cookie)
    # merge headers
    for k, values in exception.__dict__.pop('headers').items():
        for v in values:
            response.headers.add(k, v)
    # copy the rest
    response.__dict__.update(exception.__dict__)


def turn_socket_error_into_50X(website, exception, _=lambda a: a, response=None):
    # The mangopay module reraises exceptions and stores the original in `__cause__`.
    exception = getattr(exception, '__cause__', exception)
    if isinstance(exception, Timeout) or 'timeout' in str(exception).lower():
        response = response or Response()
        response.code = 504
    elif isinstance(exception, (socket.error, ConnectionError)):
        response = response or Response()
        response.code = 502
    else:
        return
    response.body = _(
        "Processing your request failed because our server was unable to communicate "
        "with a service located on another machine. This is a temporary issue, please "
        "try again later."
    )
    return {'response': response, 'exception': None}


def bypass_csp_for_form_redirects(response, state, website, request=None):
    if request is None:
        return
    # https://github.com/liberapay/liberapay.com/issues/952
    if response.code == 302:
        target = response.headers[b'Location']
        is_internal = (
            target[:1] in (b'/', b'.') or
            target.startswith(b'%s://%s/' % (
                website.canonical_scheme.encode('ascii'), request.headers[b'Host']
            ))
        )
        if is_internal:
            # Not an external redirect
            return
        response.code = 200
        url = response.headers.pop(b'Location').decode('ascii')
        try:
            response.refresh(state, interval=0, url=url)
        except Response:
            pass


def delegate_error_to_simplate(website, state, response, request=None, resource=None):
    """
    Wrap Pando's function to avoid dispatching to `error.spt` if the response is
    already a complete error page.
    """
    if b'Content-Type' in response.headers:
        return  # this response is already completely rendered
    return pando.state_chain.delegate_error_to_simplate(
        website, state, response, request, resource
    )


def return_500_for_exception(website, exception, response=None):
    response = response or Response()
    response.code = 500
    if website.show_tracebacks:
        import traceback
        response.body = traceback.format_exc()
    else:
        response.body = (
            "Uh-oh, you've found a serious bug. Sorry for the inconvenience, "
            "we'll get it fixed ASAP."
        )
    return {'response': response, 'exception': None}


def overwrite_status_code_of_gateway_errors(response):
    """This function changes 502 and 504 response codes to 500.

    Why? Because CloudFlare masks our error page if we return a 502 or 504:
    https://github.com/liberapay/liberapay.com/issues/592
    """
    if response.code in (502, 504):
        response.code = 500
