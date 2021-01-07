'use strict'

const INSTAGRAM_BASE_URL = 'https://www.instagram.com'
const BRIDGY_BASE_URL = 'https://brid.gy/instagram/browser'
// const BRIDGY_BASE_URL = 'http://localhost:8080/instagram/browser'
const LOGIN_URL = 'https://www.instagram.com/accounts/login/'

/**
 * Injects mock globals for tests.
 */
function injectGlobals(newGlobals) {
  Object.assign(global, newGlobals)
}


/**
 * Polls the user's IG photos, forwards new comments and likes to Bridgy.
 */
async function poll() {
  console.log('Starting poll...')

  const data = await browser.storage.sync.get(['instagramUsername', 'token'])
  const token = data.token
  if (!token) {
    console.error('No stored token!')
    return
  }

  await browser.storage.sync.set({instagramLastStart: Date.now()})

  // extract Instagram username from a logged in home page fetch
  let username = data.instagramUsername
  if (!username) {
    username = await forward('/', '/homepage')
    if (!username)
      return
    await browser.storage.sync.set({instagramUsername: username})
  }

  // extract posts (activities) from profile fetch
  const activities = await forward(`/${username}/`, `/profile?token=${token}`)
  if (!activities)
    return

  for (const activity of activities) {
    // check cached comment and like counts for this post, skip if they're unchanged
    const shortcode = activity.object.ig_shortcode
    const comments = activity.object.replies ? activity.object.replies.totalItems : null
    const likes = activity.object.ig_like_count

    if (comments != null && likes != null) {
      const cacheKey = `instagramPost-${shortcode}`
      let cache = await browser.storage.sync.get([cacheKey])
      if (cache[cacheKey] &&
          cache[cacheKey].c == comments && cache[cacheKey].l == likes) {
        console.debug(`No new comments or likes for ${shortcode}, skipping fetches`)
        continue
      }
      cache[cacheKey] = {c: comments, l: likes}
      await browser.storage.sync.set(cache)
    }

    // fetch post permalink for comments
    const resolved = await forward(`/p/${shortcode}/`, `/post?token=${token}`)
    if (!resolved) {
      console.warn(`Bridgy couldn't translate post HTML for ${shortcode}`)
      continue
    }

    // fetch likes
    if (!await forward(`/graphql/query/?query_hash=d5d763b1e2acf209d62d22d184488e57&variables={"shortcode":"${shortcode}","include_reel":false,"first":100}`,
                       `/likes?id=${activity.id}&token=${token}`)) {
      console.warn(`Bridgy couldn't translate likes for ${shortcode}`)
      continue
    }
  }

  await postBridgy(`/poll?username=${username}`)

  await browser.storage.sync.set({instagramLastSuccess: Date.now()})
  console.log('Done!')
}


/**
 * Fetches a page from Instagram, then sends it to Bridgy.
 *
 * @param {String} instagramPath
 * @param {String} bridgyPath
 * @returns {String} Response body from Bridgy
 */
async function forward(instagramPath, bridgyPath) {
  const data = await getInstagram(instagramPath)
  if (data) {
    return await postBridgy(bridgyPath, data)
  }
}


/**
 * Finds and returns Instagram cookies that include sessionid.
 *
 * Looks through all cookie stores and contextual identities (ie containers).
 *
 * TODO: debug why this still doesn't actually work with eg the Firefox
 * Container Tabs extension. The HTTP requests complain that the sessionid
 * cookie is expired, even if it works in the container tab.
 *
 * @returns {String} Cookie header for instagram.com, ready to be sent, or null
 */
async function findCookies(path) {
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

  for (const storeId of storeIds) {
    const cookies = await browser.cookies.getAll({
      storeId: storeId,
      domain: 'instagram.com',
    })
    if (cookies) {
      const header = cookies.map(c => `${c.name}=${c.value}`).join('; ')
      // console.debug(header)
      if (header.includes('sessionid=')) {
        return header
      }
    }
  }

  console.log('No Instagram sessionid cookie found!')
}


/**
 * Makes an HTTP GET request to Instagram.
 *
 * @param {String} path
 * @returns {String} Response body from Instagram
 */
async function getInstagram(path) {
  const cookies = await findCookies()
  if (!cookies) {
    return
  }

  // Make HTTP request
  const url = `${INSTAGRAM_BASE_URL}${path}`
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
  const res = await fetch(url, {
    method: 'POST',
    body: body,
  })

  console.debug(`Got ${res.status}`)
  if (res.ok) {
    var json
    try {
      json = await res.json()
    } catch (err) {
      console.error(err)
      return null
    }
    console.debug(json)
    return json
  } else {
    console.debug(await res.text())
  }
}

export {
  findCookies,
  forward,
  injectGlobals,
  poll,
  BRIDGY_BASE_URL,
  INSTAGRAM_BASE_URL,
  LOGIN_URL,
}
