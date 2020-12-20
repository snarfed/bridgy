'use strict'

const INSTAGRAM_BASE_URL = 'https://www.instagram.com'
const BRIDGY_BASE_URL = 'https://brid.gy/instagram/browser'


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
  const data = await browser.storage.sync.get()
  if (!data.instagram || !data.instagram.username) {
    const username = await forward('/', '/homepage')
    await browser.storage.sync.set({instagram: {username: username}})
  }

  const activities = await forward(`/${data.instagram.username}/`, '/profile')
  if (!activities) {
    return
  }

  for (const activity of JSON.parse(activities)) {
    await forward(`/p/${activity.object.ig_shortcode}/`, '/post')
    await forward(`/graphql/query/?query_hash=d5d763b1e2acf209d62d22d184488e57&variables={"shortcode":"${activity.object.ig_shortcode}","include_reel":false,"first":100}`, `/likes?id=${activity.id}`)
  }

  await postBridgy('/poll', data.instagram.username)
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
  return await postBridgy(bridgyPath, data)
}

/**
 * Makes an HTTP GET request to Instagram.
 *
 * @param {String} path
 * @returns {String} Response body from Instagram
 */
async function getInstagram(path) {
  // Fetch from Instagram
  // TODO: fetch cookies from all stores, not just the default one, in order to
  // support containers.
  // https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/Work_with_the_Cookies_API#Cookie_stores
  // https://hacks.mozilla.org/2017/10/containers-for-add-on-developers/
  // https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/API/cookies/getAll
  const cookies = await browser.cookies.getAll({domain: 'instagram.com'})
  const url = `${INSTAGRAM_BASE_URL}${path}`
  console.debug(`Fetching ${url}`)

  const res = await fetch(url, {
    method: 'GET',
    headers: {
      'Cookie': cookies.map(c => `${c.name}=${c.value}`).join('; '),
      'User-Agent': navigator.userAgent,
    },
    // required for sending cookies in older browsers?
    // https://developer.mozilla.org/en-US/docs/Web/API/Fetch_API#Differences_from_jQuery
    credentials: 'same-origin',
  })

  console.debug(`Got ${res.status} ${res.statusText}`)
  if (!res.ok) {
    return null
  }
  return await res.text()
}

/**
 * Makes an HTTP POST request to Bridgy.
 *
 * @param {String} path
 * @param {String} body
 * @returns {String} Response body from Bridgy
 */
async function postBridgy(path, body) {
  const url = `${BRIDGY_BASE_URL}${path}`
  console.debug(`Sending to ${url}`)
  const res = await fetch(url, {
    method: 'POST',
    body: body,
  })

  console.debug(`Got ${res.status} ${res.statusText}`)
  if (res.ok) {
    return await res.text()
  }
}

// console.log('Starting')
// fetchJson().then((res) => {
//   console.log(`Got ${res.items.length} items`)
//   console.log(`${res.items[0].properties.content[0]}`)
// })

export {forward, poll, injectGlobals}
