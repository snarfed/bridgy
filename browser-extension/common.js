'use strict'

const BRIDGY_BASE_URL = 'https://brid.gy'
// const BRIDGY_BASE_URL = 'http://localhost:8080'
const INDIEAUTH_START = `${BRIDGY_BASE_URL}/indieauth/start`


/*
 * Initial setup: generate a token, then start IndieAuth flow on Bridgy to log
 * into their web site and connect that token.
 *
 * @param {Boolean} force whether to start the IndieAuth flow even if the token
 *   already exists.
 */
async function login(force) {
  let token = (await browser.storage.sync.get(['token'])).token
  let generated = false
  if (!token) {
    token = Math.random().toString(36).substring(2, 15)
    await browser.storage.sync.set({token: token})
    console.log(`Generated new token: ${token}.`)
    generated = true
  }

  if (generated || force) {
    console.log('Starting IndieAuth flow on server.')
    await browser.tabs.create({url: `${INDIEAUTH_START}?token=${token}`})
  }
}


/**
 * Abstract base class for a silo, eg Facebook or Instagram.
 *
 * See below class declaration for class static properties.
 */
class Silo {
  /**
   * Returns the URL path to the user's profile, eg '/snarfed'.
   *
   * To be implemented by subclasses.
   *
   * @returns {String} URL path to the user's silo profile
   */
  static async profilePath() {
    throw new Error('Not implemented')
  }

  /**
   * Returns the URL path to the user's feed of posts.
   *
   * To be implemented by subclasses.
   *
   * @returns {String} URL path
   */
  static async feedPath() {
    throw new Error('Not implemented')
  }

  /**
   * Returns an AS1 activity's reaction count, if available.
   *
   * To be implemented by subclasses.
   *
   * @param {Object} AS1 activity
   * @returns {integer} number of reactions for this activity
   */
  static reactionsCount(activity) {
    throw new Error('Not implemented')
  }

  /**
   * Returns the URL path for a given AS1 activity's reactions.
   *
   * To be implemented by subclasses.
   *
   * @param {Object} AS1 activity
   * @returns {String} silo URL path
   */
  static reactionsPath(activity) {
    throw new Error('Not implemented')
  }

  /**
   * Polls the user's posts, forwards new comments and likes to Bridgy.
   */
  static async poll() {
    if (await this.storageGet('enabled') == false) {
      return
    }

    const token = (await browser.storage.sync.get(['token'])).token
    if (!token) {
      return
    }

    const status = await this.postBridgy(`/status`)
    if (status && status['poll-seconds']) {
      const mins = status['poll-seconds'] / 60
      // this overwrites the existing alarm.
      // https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/API/alarms/create
      console.log(`Scheduling ${this.NAME} poll every ${mins}m`)
      browser.alarms.create(this.alarmName(), {
        delayInMinutes: mins,
        periodInMinutes: mins,
      })
    }
    if (status && status.status == 'disabled') {
      return
    }

    console.log('Starting poll...')
    await this.storageSet('lastStart', Date.now())

    // register with Bridgy (ie create source for this silo account) if necessary
    let key = await this.storageGet('bridgySourceKey')
    if (!key) {
      key = await this.forward(await this.profilePath(), '/profile')
      await this.storageSet('bridgySourceKey', key)
    }

    // extract posts (activities) from profile timeline
    const activities = await this.forward(await this.feedPath(), `/feed`)
    if (!activities) {
      return
    }

    for (const activity of activities) {
      // check cached comment and like counts for this post, skip if they're unchanged
      const commentCount = activity.object.replies ? activity.object.replies.totalItems : null
      const reactionCount = this.reactionsCount(activity)
      const cacheKey = `post-${activity.id}`
      if (commentCount != null && reactionCount != null) {
        let cache = await this.storageGet(cacheKey)
        if (cache && cache.c == commentCount && cache.r == reactionCount) {
          console.debug(`No new comments or reactions for ${activity.id}, skipping`)
          continue
        }
      }

      // fetch post permalink for comments
      const resolved = await this.forward(activity.url, `/post`)
      if (!resolved) {
        console.warn(`Bridgy couldn't translate post HTML`)
        continue
      }

      // fetch reactions
      const reactions = await this.forward(this.reactionsPath(activity),
                                           `/reactions?id=${activity.id}`)
      if (!reactions) {
        console.warn(`Bridgy couldn't translate reactions`)
        continue
      }

      const numComments = (resolved.object && resolved.object.replies &&
                           resolved.object.replies.items)
          ? resolved.object.replies.items.length : 0
      await this.storageSet(cacheKey, {c: numComments, r: reactions.length})

      if (numComments > 0 || reactions.length > 0) {
        await this.storageSet('lastResponse', Date.now())
      }
    }

    await this.postBridgy(`/poll`)
    await this.storageSet('lastSuccess', Date.now())
    if (!(await this.storageGet('lastResponse'))) {
      await this.storageSet('lastResponse', Date.now())
    }
    console.log('Done!')
  }

  /**
   * Finds and returns session cookies for this silo.
   *
   * Looks through all cookie stores and contextual identities (ie containers).
   *
   * TODO: debug why this still doesn't actually work with eg the Firefox
   * Container Tabs extension. The HTTP requests complain that the session
   * cookie is expired, even if it works in the container tab.
   *
   * @returns {String} Cookie header for the silo, ready to be sent, or null
   */
  static async findCookies(path) {
    // getAllCookieStores() only returns containers with open tabs, so we have to
    // use the contextualIdentities API to get any others, eg Firefox container tabs.
    // https://bugzilla.mozilla.org/show_bug.cgi?id=1486274
    let storeIds = (await browser.cookies.getAllCookieStores()).map(s => s.id)

    // this needs the contextualIdentities permission, which we don't currently
    // include in manifest.json since it's not supported in Chrome.
    if (browser.contextualIdentities) {
        storeIds = storeIds.concat(
          (await browser.contextualIdentities.query({})).map(s => s.cookieStoreId))
    }

    if (storeIds.find(id => id.startsWith('firefox-container-'))) {
      console.debug('Detected active Firefox Container add-on!')
    }

    for (const storeId of storeIds) {
      const cookies = await browser.cookies.getAll({
        storeId: storeId,
        domain: this.DOMAIN,
      })
      if (cookies) {
        const header = cookies.map(c => `${c.name}=${c.value}`).join('; ')
        // console.debug(header)
        if (header.includes(`${this.COOKIE}=`)) {
          console.debug(`Using ${this.NAME} cookie ${header}`)
          return header
        }
      }
    }

    console.log(`No ${this.NAME} ${this.COOKIE} cookie found!`)
  }

  /**
   * Fetches a silo page, then sends it to Bridgy.
   *
   * @param {String} siloPath
   * @param {String} bridgyPath
   * @returns {String} Response body from Bridgy
   */
  static async forward(siloPath, bridgyPath) {
    if (!siloPath || !bridgyPath) {
      return
    }
    const data = await this.siloGet(siloPath)
    if (data) {
      return await this.postBridgy(bridgyPath, data)
    }
  }

  /**
   * WebRequest onBeforeSendHeaders listener that injects cookies.
   *
   * Needed to support Firefox container tabs.
   *
   * https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/API/webRequest/onBeforeSendHeaders
   */
  static injectCookies(cookies) {
    return function (details) {
      for (let header of details.requestHeaders) {
        if (header.name == 'X-Bridgy') {
          header.name = 'Cookie'
          header.value = cookies
          return details
        }
      }
    }
  }

  /**
   * Makes an HTTP GET request to the silo.
   *
   * @param {String} url
   * @returns {String} Response body from the silo
   */
  static async siloGet(url) {
    // Set up cookies. Can't pass them to fetch directly because it blocks the
    // Cookie header. :( Instead, we use webRequest, which lets us.
    // https://developer.mozilla.org/en-US/docs/Glossary/Forbidden_header_name
    // https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/API/webRequest/onBeforeSendHeaders
    //
    // (If only the fetch API let us specify a cookie store, we could skip all
    // this and just let it automatically send the appropriate cookies from that
    // store. We already have the contextualIdentities permission, so we already
    // have access to those cookies. Maybe because contextualIdentities isn't a
    // cross-browser standard yet? Argh!)
    const cookies = await this.findCookies()
    if (!cookies) {
      return
    }

    const inject = this.injectCookies(cookies)
    if (!browser.webRequest.onBeforeSendHeaders.hasListener(inject)) {
      browser.webRequest.onBeforeSendHeaders.addListener(
        inject,
        {urls: [`${this.BASE_URL}/*`]},
        ['blocking', 'requestHeaders']
      );
    }

    // check if url is a full URL or a path
    try {
      const parsed = new URL(url)
      if (parsed.hostname != this.DOMAIN &&
          !parsed.hostname.endsWith(`.${this.DOMAIN}`)) {
        console.error(`Got non-${this.NAME} URL: ${url}`)
        return
      }
    } catch (err) {
      url = `${this.BASE_URL}${url}`
    }

    // Make HTTP request
    console.debug(`Fetching ${url}`)
    const res = await fetch(url, {
      method: 'GET',
      redirect: 'follow',
      // replaced in injectCookies()
      headers: {'X-Bridgy': '1'},
    })

    console.debug(`Got ${res.status}`)
    const text = await res.text()
    console.debug(text)
    if (res.ok) {
      return text
    }
  }

  /**
   * Makes an HTTP POST request to Bridgy.
   *
   * @param {String} path_query
   * @param {String} body
   * @returns {Object} JSON parsed response from Bridgy
   */
  static async postBridgy(path_query, body) {
    const token = (await browser.storage.sync.get(['token'])).token
    if (!token) {
      console.error('No stored token!')
      return
    }

    let url = new URL(`${BRIDGY_BASE_URL}/${this.NAME}/browser${path_query}`)
    url.searchParams.set('token', token)

    const key = await this.storageGet('bridgySourceKey')
    if (key) {
      url.searchParams.set('key', key)
    }

    console.debug(`Sending to ${url}`)
    try {
      // TODO: support optional timeout via signal and AbortHandler
      // https://dmitripavlutin.com/timeout-fetch-request/
      const res = await fetch(url.toString(), {
        method: 'POST',
        body: body,
      })
      console.debug(`Got ${res.status}`)
      if (res.ok) {
        let json = await res.json()
        console.debug(json)
        return json
      } else {
        console.debug(await res.text())
      }
    } catch (err) {
      console.error(err)
      return null
    }
  }

  /**
   * Fetches a value from local storage for a key prefixed with this silo's name.
   *
   * For example, storageGet('foo') would return the value for key 'NAME-foo'.
   *
   * @param {String} key
   * @returns {Object} stored value, or none
   */
  static async storageGet(key) {
    key = this.siloKey(key)
    return (await browser.storage.local.get([key]))[key]
  }

  /**
   * Stores a value from local storage for a key prefixed with this silo's name.
   *
   * For example, storageSet('foo') would store the value for key 'NAME-foo'.
   *
   * @param {String} key
   * @param {Object} value
   */
  static async storageSet(key, value) {
    return await browser.storage.local.set({[this.siloKey(key)]: value})
  }

  /**
   * Prefixes a local storage key with the silo's name.
   *
   * @param {String} key
   * @returns {String} prefixed key
   */
  static siloKey(key) {
    return `${this.NAME}-${key}`
  }

  /**
   * Returns the name of the poll alarm for this silo.
   *
   * @returns {String} alarm name
   */
  static alarmName() {
    return `bridgy-${this.NAME}-poll`
  }
}

Silo.DOMAIN = null     // eg 'silo.com'
Silo.NAME = null       // eg 'instagram'
Silo.BASE_URL = null   // eg 'https://silo.com'
Silo.LOGIN_URL = null  // eg 'https://silo.com/login'
Silo.COOKIE = null     // eg 'sessionid'


export {
  BRIDGY_BASE_URL,
  INDIEAUTH_START,
  login,
  Silo,
}
