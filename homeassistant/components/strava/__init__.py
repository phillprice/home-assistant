from datetime import datetime, timedelta
import logging
import voluptuous as vol
from homeassistant.components.http import HomeAssistantView
import homeassistant.helpers.config_validation as cv
from homeassistant.util import Throttle

_LOGGER = logging.getLogger(__name__)

MIN_TIME_BETWEEN_UPDATES = timedelta(seconds=10)

CONF_CLIENT_ID = 'client_id'
CONF_CLIENT_SECRET = 'client_secret'

AUTH_CALLBACK_NAME = 'api:strava'
AUTH_CALLBACK_PATH = '/api/strava'

CONFIGURATOR_DESCRIPTION = "To link your Strava account, " \
                           "click the link, login, and authorize:"
CONFIGURATOR_LINK_NAME = "Link Strava account"
CONFIGURATOR_SUBMIT_CAPTION = "I authorized successfully"

DEFAULT_NAME = 'Strava'

DOMAIN = 'strava'

STORAGE_KEY = DOMAIN
STORAGE_VERSION = 1

CONFIG_SCHEMA = vol.Schema({
    DOMAIN: vol.Schema({
        vol.Required(CONF_CLIENT_ID): cv.string,
        vol.Required(CONF_CLIENT_SECRET): cv.string,
    })
}, extra=vol.ALLOW_EXTRA)


##
## @brief      { function_description }
##
## @param      hass    The hass
## @param      config  The configuration
##
## @return     { description_of_the_return_value }
##
async def async_setup(hass, config):
    data = StravaData(hass, config.get(DOMAIN))

    if not data.is_authorized:
        await data.get_token()

    hass.data[DOMAIN] = data

    return True


class StravaData:
    """
    A model which stores the Strava data.
    """

    ##
    ## @brief      Constructs the object.
    ##
    ## @param      self    The object
    ## @param      hass    The hass
    ## @param      config  The configuration
    ##
    def __init__(self, hass, config):
        from stravalib.client import Client

        self.client = Client()
        self._configurator = None
        self._token = None
        self._hass = hass
        self._config = config

        self._hass.http.register_view(StravaAuthCallbackView(self))

        self._client_id = config.get(CONF_CLIENT_ID)
        self._client_secret = config.get(CONF_CLIENT_SECRET)

        self.athletes = {}
        self.gears = {}
        self.clubs = {}

    @property
    def is_authorized(self):
        """
        Check if there is a (possiblly expired) OAuth2 token.
        
        @param      self  The object
        
        @return     True if authorized, False otherwise.
        """
        return self._token is not None

    @property
    def is_token_valid(self):
        """
        Check if OAuth2 token is present and not expired.
        
        @param      self  The object
        
        @return     True if token valid, False otherwise.
        """
        if not self.is_authorized:
            _LOGGER.info("Not authorized")
            return False

        expires_at = datetime.fromtimestamp(self._token['expires_at'])
        if expires_at > datetime.now() + 300:
            return True

        _LOGGER.info("Token expired: %s", repr(self._token))
        return False

    async def get_token(self):
        """
        Load the OAuth2 token from the store.
        
        @param      self  The object
        
        @return     The token.
        """
        if not self.is_authorized:
            store = self._hass.helpers.storage.Store(STORAGE_VERSION,
                                                     STORAGE_KEY)
            self._token = await store.async_load()

            if self._token:
                self.client.access_token = self._token['access_token']
            else:
                _LOGGER.info("Requesting token")
                await self.request_token()
                return

        expires_at = datetime.fromtimestamp(self._token['expires_at'])
        if expires_at < datetime.now():
            await self.refresh_token()

    async def authorize(self, code, hass):
        """
        Request initial authorization.
        
        @param      self  The object
        @param      code  The code
        @param      hass  The hass
        
        @return     { description_of_the_return_value }
        """
        self._token = await hass.async_add_executor_job(
            self.client.exchange_code_for_token,
            self._client_id,
            self._client_secret,
            code
        )

        store = hass.helpers.storage.Store(STORAGE_VERSION, STORAGE_KEY)
        await store.async_save(self._token)

        if self.is_authorized:
            await hass.async_add_executor_job(
                hass.components.configurator.request_done,
                self._configurator
            )
            del self._configurator

        await async_setup(hass, self._config)

    async def request_token(self):
        """
        Request Strava access token.
        
        @param      self  The object
        
        @return     { description_of_the_return_value }
        """
        callback_url = '{}{}'.format(self._hass.config.api.base_url,
                                     AUTH_CALLBACK_PATH)
        authorize_url = self.client.authorization_url(
            client_id=self._config.get(CONF_CLIENT_ID),
            redirect_uri=callback_url)

        self._configurator = \
            self._hass.components.configurator.async_request_config(
                DEFAULT_NAME, lambda _: None,
                link_name=CONFIGURATOR_LINK_NAME,
                link_url=authorize_url,
                description=CONFIGURATOR_DESCRIPTION,
                submit_caption=CONFIGURATOR_SUBMIT_CAPTION)

    async def refresh_token(self):
        """
        Renew Strava access token.
        
        @param      self  The object
        
        @return     { description_of_the_return_value }
        """
        self._token = await self._hass.async_add_executor_job(
            self.client.refresh_access_token,
            self._client_id,
            self._client_secret,
            self._token['refresh_token'])

        store = self._hass.helpers.storage.Store(STORAGE_VERSION, STORAGE_KEY)
        await store.async_save(self._token)

    def get_athlete(self, id):
        """
        Get existing Athlete model or create if not existing.
        
        @param      self  The object
        @param      id    The identifier
        
        @return     The athlete.
        """
        if id not in self.athletes:
            self.athletes[id] = StravaAthleteData(self, id)

        return self.athletes[id]

    def get_gear(self, id):
        """
        Get existing Gear model or create if not existing.
        
        @param      self  The object
        @param      id    The identifier
        
        @return     The gear.
        """
        if id not in self.gears:
            self.gears[id] = StravaGearData(self, id)

        return self.gears[id]

    def get_club(self, id):
        """
        Get existing Club model or create if not existing.
        
        @param      self  The object
        @param      id    The identifier
        
        @return     The club.
        """
        if id not in self.clubs:
            self.clubs[id] = StravaClubData(self, id)

        return self.clubs[id]


##
## @brief      Class for strava athlete data.
##
class StravaAthleteData:

    ##
    ## @brief      Constructs the object.
    ##
    ## @param      self  The object
    ## @param      data  The data
    ## @param      id    The identifier
    ##
    def __init__(self, data, id=None):
        self.id = id
        self.data = data

        self.details = None
        self.stats = None
        self.last_activity = None

    ##
    ## @brief      { function_description }
    ##
    ## @param      self  The object
    ## @param      hass  The hass
    ##
    ## @return     { description_of_the_return_value }
    ##
    async def update_last_actitivity(self, hass):
        ##
        ## @brief      Gets the last activity.
        ##
        ## @param      client  The client
        ##
        ## @return     The last activity.
        ##
        def get_last_activity(client):
            activities = client.get_activities(limit=1)
            last = next(activities)
            detailed = client.get_activity(last.id, True)

            return detailed

        self.last_activity = await hass.async_add_executor_job(
            get_last_activity, self.data.client)

        _LOGGER.info("Fetched last activity")

    ##
    ## @brief      { function_description }
    ##
    ## @param      self  The object
    ## @param      hass  The hass
    ##
    ## @return     { description_of_the_return_value }
    ##
    async def update_details(self, hass):
        self.details = await hass.async_add_executor_job(
            self.data.client.get_athlete, self.id)

        _LOGGER.info("Fetched athlete details")

    ##
    ## @brief      { function_description }
    ##
    ## @param      self  The object
    ## @param      hass  The hass
    ##
    ## @return     { description_of_the_return_value }
    ##
    async def update_stats(self, hass):
        self.stats = await hass.async_add_executor_job(
            self.data.client.get_athlete_stats, self.id)

        _LOGGER.info("Fetched athlete stats")

    ##
    ## @brief      { function_description }
    ##
    ## @param      self  The object
    ## @param      hass  The hass
    ##
    ## @return     { description_of_the_return_value }
    ##
    @Throttle(MIN_TIME_BETWEEN_UPDATES)
    async def update(self, hass):
        import asyncio

        # Request or refresh token
        await self.data.get_token()

        await asyncio.gather(
            self.update_last_actitivity(hass),
            self.update_details(hass),
            self.update_stats(hass)
        )


##
## @brief      Class for strava club data.
##
class StravaClubData:

    ##
    ## @brief      Constructs the object.
    ##
    ## @param      self  The object
    ## @param      data  The data
    ## @param      id    The identifier
    ##
    def __init__(self, data, id):
        self.id = id
        self.data = data

        self.club = None

    ##
    ## @brief      { function_description }
    ##
    ## @param      self  The object
    ## @param      hass  The hass
    ##
    ## @return     { description_of_the_return_value }
    ##
    @Throttle(MIN_TIME_BETWEEN_UPDATES)
    async def update(self, hass):
        await self.data.get_token()
        self.club = await hass.async_add_executor_job(
            self.data.client.get_club, self.id)


##
## @brief      Class for strava gear data.
##
class StravaGearData:

    ##
    ## @brief      Constructs the object.
    ##
    ## @param      self  The object
    ## @param      data  The data
    ## @param      id    The identifier
    ##
    def __init__(self, data, id):
        self.id = id
        self.data = data

        self.gear = None

    ##
    ## @brief      { function_description }
    ##
    ## @param      self  The object
    ## @param      hass  The hass
    ##
    ## @return     { description_of_the_return_value }
    ##
    @Throttle(MIN_TIME_BETWEEN_UPDATES)
    async def update(self, hass):
        await self.data.get_token()
        self.gear = await hass.async_add_executor_job(
            self.data.client.get_gear, self.id)


class StravaAuthCallbackView(HomeAssistantView):
    """
    Strava Authorization Callback View.
    """

    requires_auth = False
    url = AUTH_CALLBACK_PATH
    name = AUTH_CALLBACK_NAME

    ##
    ## @brief      Constructs the object.
    ##
    ## @param      self  The object
    ## @param      data  The data
    ##
    def __init__(self, data):
        self._data = data

    ##
    ## @brief      { function_description }
    ##
    ## @param      self     The object
    ## @param      request  The request
    ##
    ## @return     { description_of_the_return_value }
    ##
    async def get(self, request):
        hass = request.app['hass']
        code = request.query['code']

        await self._data.authorize(code, hass)
