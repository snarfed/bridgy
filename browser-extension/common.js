'use strict'

const BRIDGY_BASE_URL = 'https://brid.gy'
// const BRIDGY_BASE_URL = 'http://localhost:8080'
const INDIEAUTH_START = 'https://brid.gy/indieauth/start'
// const INDIEAUTH_START = 'http://localhost:8080/indieauth/start'


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
 */
class Silo {
  DOMAIN     // eg 'silo.com'
  NAME       // eg 'instagram'
  BASE_URL   // eg 'https://silo.com'
  LOGIN_URL  // eg 'https://silo.com/login'
  COOKIE     // eg 'sessionid'

  /**
   * Returns the URL path to the user's profile, eg '/snarfed'.
   *
   * To be implemented by subclasses.
   *
   * @returns {String} URL path to the user's silo profile
   */
  async profilePath() {
    throw new Error('Not implemented')
  }

  /**
   * Returns the URL path to the user's feed of posts.
   *
   * To be implemented by subclasses.
   *
   * @returns {String} URL path
   */
  async feedPath() {
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
  reactionsCount(activity) {
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
  reactionsPath(activity) {
    throw new Error('Not implemented')
  }

  /**
   * Polls the user's posts, forwards new comments and likes to Bridgy.
   */
  async poll() {
    const token = (await browser.storage.sync.get(['token'])).token
    if (!token) {
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

      if (commentCount != null && reactionCount != null) {
        const cacheKey = `post-${activity.id}`
        let cache = await this.storageGet(cacheKey)
        if (cache && cache.c == commentCount && cache.r == reactionCount) {
          console.debug(`No new comments or reactions for ${activity.id}, skipping`)
          continue
        }
        await this.storageSet(cacheKey, {c: commentCount, r: reactionCount})
      }

      // fetch post permalink for comments
      const resolved = await this.forward(activity.url, `/post`)
      if (!resolved) {
        console.warn(`Bridgy couldn't translate post HTML`)
        continue
      }

      // fetch reactions
      if (!await this.forward(this.reactionsPath(activity),
                              `/reactions?id=${activity.id}`)) {
        console.warn(`Bridgy couldn't translate reactions`)
        continue
      }
    }

    await this.postBridgy(`/poll`)
    await this.storageSet('lastSuccess', Date.now())
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
  async findCookies(path) {
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
  async forward(siloPath, bridgyPath) {
    if (!siloPath || !bridgyPath) {
      return
    }
    const data = await this.siloGet(siloPath)
    if (data) {
      return await this.postBridgy(bridgyPath, data)
    }
  }

  /**
   * Makes an HTTP GET request to the silo.
   *
   * @param {String} url
   * @returns {String} Response body from the silo
   */
  async siloGet(url) {
    const cookies = await this.findCookies()
    if (!cookies) {
      return
    }

    // check if url is a full URL or a path
    try {
      const parsed = new URL(url)
      if (parsed.hostname != this.DOMAIN &&
          !parsed.hostname.startsWith(`.${this.DOMAIN}`)) {
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
      headers: {
        'Cookie': cookies,
        'User-Agent': navigator.userAgent,
      },
      // required for sending cookies in older browsers?
      // https://developer.mozilla.org/en-US/docs/Web/API/Fetch_API#Differences_from_jQuery
      credentials: 'same-origin',
      redirect: 'follow',
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
  async postBridgy(path_query, body) {
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
  async storageGet(key) {
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
  async storageSet(key, value) {
    return await browser.storage.local.set({[this.siloKey(key)]: value})
  }

  /**
   * Prefixes a local storage key with the silo's name.
   *
   * @param {String} key
   * @returns {String} prefixed key
   */
  siloKey(key) {
    return `${this.NAME}-${key}`
  }
}


export {
  BRIDGY_BASE_URL,
  INDIEAUTH_START,
  login,
  Silo,
}
