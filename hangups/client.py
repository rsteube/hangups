"""Abstract class for writing chat clients."""

from tornado import ioloop, gen, httpclient, httputil
import hashlib
import http.cookies
import json
import logging
import random
import re
import time

from hangups import javascript, longpoll


logger = logging.getLogger(__name__)


@gen.coroutine
def _fetch(url, method='GET', params=None, headers=None, cookies=None,
           data=None, streaming_callback=None):
    """Wrapper for tornado.httpclient.AsyncHTTPClient.fetch."""
    if headers is None:
        headers = {}
    if params is not None:
        url = httputil.url_concat(url, params)
    if cookies is not None:
        # abuse SimpleCookie to escape our cookies for us
        simple_cookies = http.cookies.SimpleCookie(cookies)
        headers['cookie'] = '; '.join(val.output(header='')[1:]
                                      for val in simple_cookies.values())
    http_client = httpclient.AsyncHTTPClient()
    # set the timeout nice and nice for long-polling
    res = yield http_client.fetch(httpclient.HTTPRequest(
        httputil.url_concat(url, params), method=method,
        headers=httputil.HTTPHeaders(headers), body=data,
        streaming_callback=streaming_callback, request_timeout=60*60
    ))
    return res


class HangupsClient(object):
    """Abstract class for writing chat clients.

    Designed to allow building a chat client by subclassing the abstract
    methods. If __init__ is subclassed, super().__init__() must be called.

    This is only low-level abstraction around the API.
    """

    def __init__(self):
        self._cookies = None
        self._origin_url = None
        self._push_parser = None

        # discovered automatically:

        self._initial_conversations = None
        self._initial_contacts = None
        self._self_user_ids = None
        # the api key sent with every request
        self._api_key = None
        # fields sent in request headers
        self._header_date = None
        self._header_version = None
        self._header_id = None
        self._header_client = None
        # parameters related talkgadget channel requests
        self._channel_path = None
        self._gsessionid = None
        self._clid = None
        self._channel_ec_param = None
        self._channel_prop_param = None
        self._channel_session_id = None

    ##########################################################################
    # Abstract methods
    ##########################################################################

    @gen.coroutine
    def on_event(self, event):
        """Abstract method called with new events."""
        pass

    @gen.coroutine
    def on_connect(self, conversations, contacts, self_user_ids):
        """Abstract method called when push connection is established."""
        pass

    @gen.coroutine
    def on_disconnect(self):
        """Abstract method called when push connection is disconnected."""
        pass

    ##########################################################################
    # Public methods
    ##########################################################################

    @gen.coroutine
    def get_users(self, user_ids_list):
        """Look up a list of users by their IDs."""
        chat_ids = [u[0] for u in user_ids_list] # TODO should be chat_id
        res = yield self._getentitybyid(chat_ids)
        # TODO handle users that are not found
        users = {}
        for entity in res['entity_result']:
            entity = entity['entity'][0]
            user_ids = (entity['id']['chat_id'], entity['id']['gaia_id'])
            first_name = entity['properties']['first_name']
            full_name = entity['properties']['display_name']
            users[user_ids] = {
                'first_name': first_name,
                'full_name': full_name,
            }
        return users

    @gen.coroutine
    def send_message(self, conversation_id, text):
        """Send a message to a conversation."""
        yield self._sendchatmessage(conversation_id, text)

    @gen.coroutine
    def connect(self, cookies, origin_url='https://talkgadget.google.com'):
        """Initialize to gather connection parameters."""
        self._cookies = cookies
        self._origin_url = origin_url
        yield self._init_talkgadget_1()
        yield self.on_connect(self._initial_conversations,
                              self._initial_contacts,
                              self._self_user_ids)

    @gen.coroutine
    def run_forever(self):
        """Make repeated long-polling requests to receive events.

        This method only returns when the connection has been closed due to an
        error.
        """
        MAX_RETRIES = 1  # maximum number of times to retry after a failure
        retries = MAX_RETRIES # number of remaining retries
        need_new_sid = True  # whether a new SID is needed

        while retries >= 0:
            # Request a new SID if we don't have one yet, or the previous one
            # became invalid.
            if need_new_sid:
                # TODO: error handling
                # TODO: might need to sync any missed events
                yield self._fetch_channel_sid()
                need_new_sid = False
            # Clear any previous push data, since if there was an error it
            # could contain garbage.
            self._push_parser = longpoll.PushDataParser()
            try:
                yield self._longpoll_request()
            except httpclient.HTTPError as e:
                # An error occurred, so decrement the number of retries.
                retries -= 1
                if e.code == 400 and e.response.reason == 'Unknown SID':
                    logger.error('Long-polling request failed because SID '
                                 'became invalid. Will attempt to recover.')
                    need_new_sid = True
                elif e.code == 599:
                    logger.error('Long-polling request failed because '
                                 'connection was closed. Will attempt to '
                                 'recover.')
                    # TODO: exponential backoff
                else:
                    logger.error('Long-polling request failed for unknown '
                                 'reason: {}'.format(e))
                    break # Do not retry.
            else:
                # The connection closed successfully, so reset the number of
                # retries.
                retries = MAX_RETRIES

        logger.error('Ran out of retries for push channel')
        yield self.on_disconnect()

    ##########################################################################
    # Private methods
    ##########################################################################

    @gen.coroutine
    def _init_talkgadget_1(self):
        """Make first talkgadget request and parse response.

        The response body is a HTML document containing a series of script tags
        containing JavaScript object. We need to parse the object to get at the
        data.
        """
        url = 'https://talkgadget.google.com/u/0/talkgadget/_/chat'
        params = {
            'prop': 'aChromeExtension',
            'fid': 'gtn-roster-iframe-id',
            'ec': '["ci:ec",true,true,false]',
        }
        headers = {
            # appears to require a browser user agent
            'user-agent': (
                'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36'
                '(KHTML, like Gecko) Chrome/34.0.1847.132 Safari/537.36'
            ),
        }
        res = yield _fetch(url, cookies=self._cookies, params=params,
                           headers=headers)
        logger.debug('First talkgadget request result:\n{}'.format(res.body))
        if res.code != 200:
            raise ValueError("First talkgadget request failed with {}: {}"
                             .format(res.code, res.body))
        res = res.body.decode()

        # Parse the response by using a regex to find all the JS objects, and
        # parsing them.
        res = res.replace('\n', '')
        regex = re.compile(
            r"(?:<script>AF_initDataCallback\((.*?)\);</script>)"
        )
        data_dict = {}
        for data in regex.findall(res):
            try:
                data = javascript.loads(data)
                data_dict[data['key']] = data['data']
            except ValueError as e:
                # not everything will be parsable, but we don't care
                logger.debug('Failed to parse JavaScript: {}\n{}'.format(e, data))

        # TODO: handle errors here
        self._api_key = data_dict['ds:7'][0][2]
        self._header_date = data_dict['ds:2'][0][4]
        self._header_version = data_dict['ds:2'][0][6]
        self._header_id = data_dict['ds:4'][0][7]
        self._channel_path = data_dict['ds:4'][0][1]
        self._clid = data_dict['ds:4'][0][7]
        self._channel_ec_param = data_dict['ds:4'][0][4]
        self._channel_prop_param = data_dict['ds:4'][0][5]

        # build dict of conversations and their participants
        self._initial_conversations = {}
        conversations = data_dict['ds:19'][0][3]
        for c in conversations:
            id_ = c[1][0][0]
            participants = c[1][13]
            last_modified = c[1][3][12]
            self._initial_conversations[id_] = {
                'participants': [],
                'last_modified': last_modified,
            }
            for p in participants:
                user_ids = tuple(p[0])
                self._initial_conversations[id_]['participants'].append(
                    user_ids
                )
        logger.info('Found {} conversations'
                    .format(len(self._initial_conversations)))

        # build dict of contacts and their names (doesn't include users not in
        # contacts)
        self._initial_contacts = {}
        contacts_main = data_dict['ds:21'][0]
        # contacts_main[2] has some, but the format is slightly different
        contacts = (contacts_main[4][2] + contacts_main[5][2] +
                    contacts_main[6][2] + contacts_main[7][2] +
                    contacts_main[8][2])
        for c in contacts:
            user_ids = tuple(c[0][8])
            full_name = c[0][9][1]
            first_name = c[0][9][2]
            self._initial_contacts[user_ids] = {
                'first_name': first_name,
                'full_name': full_name,
            }
        logger.info('Found {} contacts'.format(len(self._initial_contacts)))

        # add self to the contacts
        self_contact = data_dict['ds:20'][0][2]
        self._self_user_ids = tuple(self_contact[8])
        self._initial_contacts[self._self_user_ids] = {
            'first_name': self_contact[9][2],
            'full_name': self_contact[9][1],
        }

    @gen.coroutine
    def _fetch_channel_sid(self):
        """Request a new session ID for the push channel."""
        logger.info('Requesting new session ID...')
        url = 'https://talkgadget.google.com{}bind'.format(self._channel_path)
        params = {
            'VER': 8,
            'clid': self._clid,
            'ec': self._channel_ec_param,
            'RID': 81187, # TODO: "request ID"? should probably increment
            # Required if we want our client to be called "AChromeExtension":
            'prop': self._channel_prop_param,
        }
        res = yield _fetch(url, method='POST', cookies=self._cookies,
                           params=params, data='count=0')
        logger.debug('Fetch SID response:\n{}'.format(res.body))
        if res.code != 200:
            # TODO use better exception
            raise ValueError("Second talkgadget request failed with {}: {}"
                             .format(res.code, res.raw.read()))
        p = longpoll.PushDataParser()
        res = javascript.loads(list(p.get_submissions(res.body.decode()))[0])
        # TODO: this parsing needs to be more sophisticated
        # TODO: handle errors here
        val = res[3][1][1][1][1] # ex. foo@bar.com/AChromeExtensionBEEFBEEF
        self._header_client = val.split('/')[1] # ex. AChromeExtensionwBEEFBEEF
        self._channel_session_id = res[0][1][1]
        self._gsessionid = res[4][1][1][1][1]
        logger.info('Received new session ID: {}'
                    .format(self._channel_session_id))

    @gen.coroutine
    def _longpoll_request(self):
        """Open a long-polling request to receive push events.

        Raises HTTPError.
        """
        def streaming_callback(data):
            """Make the callback run a coroutine with exception handling."""
            future = self._on_push_data(data)
            ioloop.IOLoop.instance().add_future(future, lambda f: f.result())
        params = {
            'VER': 8,
            'clid': self._clid,
            'prop': self._channel_prop_param,
            'ec': self._channel_ec_param,
            'gsessionid': self._gsessionid,
            'RID': 'rpc',
            't': 1, # trial
            'SID': self._channel_session_id,
            'CI': 0,
        }
        URL = 'https://talkgadget.google.com/u/0/talkgadget/_/channel/bind'
        res = yield _fetch(URL, params=params, cookies=self._cookies,
                           streaming_callback=streaming_callback)
        return res


    @gen.coroutine
    def _on_push_data(self, data_bytes):
        """Parse push data and pass parsed events to on_event."""
        logger.debug('Received push data:\n{}'.format(data_bytes))
        for event in self._push_parser.get_events(data_bytes.decode()):
            logger.info('Received event: {}'.format(event))
            yield self.on_event(event)

    def _get_authorization_header(self):
        """Return autorization header for chat API request."""
        # technically, it doesn't matter what the url and time are
        time_msec = int(time.time() * 1000)
        auth_string = '{} {} {}'.format(time_msec, self._get_cookie("SAPISID"),
                                        self._origin_url)
        auth_hash = hashlib.sha1(auth_string.encode()).hexdigest()
        return 'SAPISIDHASH {}_{}'.format(time_msec, auth_hash)

    def _get_cookie(self, name):
        """Return a cookie for raise error if that cookie was not provided."""
        try:
            return self._cookies[name]
        except KeyError:
            raise KeyError("Cookie '{}' is required".format(name))

    def _get_request_header(self):
        """Return request header for chat API request."""
        return [
            [3, 3, self._header_version, self._header_date],
            [self._header_client, self._header_id],
            None,
            "en"
        ]

    @gen.coroutine
    def _request(self, endpoint, body_json):
        """Make chat API request."""
        url = 'https://clients6.google.com/chat/v1/{}'.format(endpoint)
        headers = {
            'authorization': self._get_authorization_header(),
            'x-origin': self._origin_url,
            'x-goog-authuser': '0',
            'content-type': 'application/json+protobuf',
        }
        required_cookies = ['SAPISID', 'HSID', 'SSID', 'APISID', 'SID']
        cookies = {cookie: self._get_cookie(cookie)
                   for cookie in required_cookies}
        params = {
            'key': self._api_key,
            'alt': 'json', # json or protojson
        }
        res = yield _fetch(url, method='POST', headers=headers,
                           cookies=cookies, params=params,
                           data=json.dumps(body_json))
        logger.debug('Response to request for {} was {}:\n{}'
                     .format(endpoint, res.code, res.body))
        if res.code != 200:
            raise ValueError('Request to {} endpoint failed with {}: {}'
                             .format(endpoint, res.code, res.body.decode()))
        return res

    @gen.coroutine
    def _getselfinfo(self):
        """Return information about your account."""
        res = yield self._request('contacts/getselfinfo', [
            self._get_request_header(),
            [], []
        ])
        return json.loads(res.body.decode())

    @gen.coroutine
    def _setfocus(self, conversation_id):
        """Set focus (occurs whenever you give focus to a client)."""
        res = yield self._request('conversations/setfocus', [
            self._get_request_header(),
            [conversation_id],
            1,
            20
        ])
        return json.loads(res.body.decode())

    @gen.coroutine
    def _searchentities(self, search_string, max_results):
        """Search for people."""
        res = yield self._request('contacts/searchentities', [
            self._get_request_header(),
            [],
            search_string,
            max_results
        ])
        return json.loads(res.body.decode())

    @gen.coroutine
    def _querypresence(self, chat_id):
        """Check someone's presence status."""
        res = yield self._request('presence/querypresence', [
            self._get_request_header(),
            [
                [chat_id]
            ],
            [1, 2, 5, 7, 8]
        ])
        return json.loads(res.body.decode())

    @gen.coroutine
    def _getentitybyid(self, chat_id_list):
        """Return information about a list of contacts."""
        res = yield self._request('contacts/getentitybyid', [
            self._get_request_header(),
            None,
            [[str(chat_id)] for chat_id in chat_id_list],
        ])
        return json.loads(res.body.decode())

    @gen.coroutine
    def _getconversation(self, conversation_id, num_events,
                         storage_continuation_token, event_timestamp):
        """Return data about a conversation.

        Seems to require both a timestamp and a token from a previous event
        """
        res = yield self._request('conversations/getconversation', [
            self._get_request_header(),
            [
                [conversation_id], [], []
            ],
            True, True, None, num_events,
            [None, storage_continuation_token, event_timestamp]
        ])
        return json.loads(res.body.decode())

    @gen.coroutine
    def _syncallnewevents(self, after_timestamp):
        """List all events occuring at or after timestamp."""
        res = yield self._request('conversations/syncallnewevents', [
            self._get_request_header(),
            after_timestamp,
            [], None, [], False, [],
            1048576
        ])
        return json.loads(res.body.decode())

    @gen.coroutine
    def _syncrecentconversations(self):
        """List the contents of recent conversations, including messages.

        Similar to syncallnewevents, but appears to return a limited number of
        conversations (20) rather than all conversations in a given date range.
        """
        res = yield self._request('conversations/syncrecentconversations', [
            self._get_request_header(),
        ])
        return json.loads(res.body.decode())

    @gen.coroutine
    def _sendchatmessage(self, conversation_id, message, is_bold=False,
                         is_italic=False, is_strikethrough=False,
                         is_underlined=False):
        """Send a chat message to a conversation."""
        client_generated_id = random.randint(0, 2**32)
        res = yield self._request('conversations/sendchatmessage', [
            self._get_request_header(),
            None, None, None, [],
            [
                [
                    [0, message, [is_bold, is_italic, is_strikethrough,
                                  is_underlined]]
                ],
                []
            ],
            None,
            [
                [conversation_id], client_generated_id, 2
            ],
            None, None, None, []
        ])
        return json.loads(res.body.decode())
