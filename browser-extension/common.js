'use strict'

const BRIDGY_BASE_URL = 'https://brid.gy/instagram/browser'
// const BRIDGY_BASE_URL = 'http://localhost:8080/instagram/browser'
const INDIEAUTH_START = 'https://brid.gy/indieauth/start'
// const INDIEAUTH_START = 'http://localhost:8080/indieauth/start'


/**
 * Injects mock globals for tests.
 */
function injectGlobals(newGlobals) {
  Object.assign(global, newGlobals)
}


/*
 * Initial setup: generate a token, then start IndieAuth flow on Bridgy to log
 * into their web site and connect that token.
 *
 * @param {Boolean} force whether to start the IndieAuth flow even if the token
 *   already exists.
 */
async function login(force) {
  const data = await browser.storage.sync.get(['token'])
  let generated = false
  let token = data.token
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
 * Makes an HTTP POST request to Bridgy.
 *
 * @param {String} path
 * @param {String} body
 * @returns {Object} JSON parsed response from Bridgy
 */
async function postBridgy(path, body) {
  const url = `${BRIDGY_BASE_URL}${path}`
  console.debug(`Sending to ${url}`)

  try {
    // TODO: support optional timeout via signal and AbortHandler
    // https://dmitripavlutin.com/timeout-fetch-request/
    const res = await fetch(url, {
      method: 'POST',
      body: body,
    })
    console.debug(`Got ${res.status}`)
    if (res.ok) {
      var json
      json = await res.json()
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
 * Abstract base class for a silo, eg Facebook or Instagram.
 */
class Silo {
  DOMAIN     // eg 'silo.com'
  BASE_URL   // eg 'https://silo.com'
  LOGIN_URL  // eg 'https://silo.com/login'
  COOKIE     // eg 'sessionid'

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
          console.debug(`Using ${this.DOMAIN} cookie ${header}`)
          return header
        }
      }
    }

    console.log(`No ${this.DOMAIN} ${this.COOKIE} cookie found!`)
  }

  /**
   * Fetches a silo page, then sends it to Bridgy.
   *
   * @param {String} siloPath
   * @param {String} bridgyPath
   * @returns {String} Response body from Bridgy
   */
  async forward(siloPath, bridgyPath) {
    const data = await this.get(siloPath)
    if (data) {
      return await postBridgy(bridgyPath, data)
    }
  }

  /**
   * Makes an HTTP GET request to the silo.
   *
   * @param {String} path
   * @returns {String} Response body from the silo
   */
  async get(path) {
    const cookies = await this.findCookies()
    if (!cookies) {
      return
    }

    // Make HTTP request
    const url = `${this.BASE_URL}${path}`
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
    })

    console.debug(`Got ${res.status}`)
    const text = await res.text()
    console.debug(text)
    if (res.ok) {
      return text
    }
  }
}


export {
  BRIDGY_BASE_URL,
  INDIEAUTH_START,
  injectGlobals,
  login,
  Silo,
}
