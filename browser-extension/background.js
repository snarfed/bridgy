'use strict'

/**
 * Injects a mock browser namespace for tests.
 */
function injectBrowser(browser) {
  global.browser = browser
}


/**
 * Polls the user's IG photos, forwards new comments and likes to Bridgy.
 */
async function poll() {
  const data = await browser.storage.sync.get()
  if (!data.instagram || !data.instagram.username) {
    let username = await forward('/', '/username')
    await forward(`/${username}/`, '/profile')
    await browser.storage.sync.set({instagram: {username: username}})
  }

  // profile
  // permalinks
  // likes
  // trigger
}


/**
 * Fetches a page from Instagram, then sends it to Bridgy.
 *
 * @param {String} instagramPath
 * @param {String} bridgyPath
 */
async function forward(instagramPath, bridgyPath) {
  // Fetch from Instagram
  // TODO: fetch cookies from all stores, not just the default one, in order to
  // support containers.
  // https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/Work_with_the_Cookies_API#Cookie_stores
  // https://hacks.mozilla.org/2017/10/containers-for-add-on-developers/
  // https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/API/cookies/getAll
  const cookies = await browser.cookies.getAll({domain: 'instagram.com'})
  let url = 'https://www.instagram.com' + instagramPath
  // console.log(`Fetching ${url}`)

  let res = await fetch(url, {
    method: 'GET',
    headers: {
      'Cookie': cookies.map(c => `${c.name}=${c.value}`).join('; '),
      'User-Agent': navigator.userAgent,
    },
    // required for sending cookies in older browsers?
    // https://developer.mozilla.org/en-US/docs/Web/API/Fetch_API#Differences_from_jQuery
    credentials: 'same-origin',
  })

  // console.log(`Got ${res.status} ${res.statusText}`)
  if (!res.ok) {
    return null
  }
  const body = await res.text()

  // Send to Bridgy
  url = 'https://brid.gy/instagram/browser' + bridgyPath
  // console.log(`Sending to ${url}`)
  res = await fetch(url, {
    method: 'POST',
    body: body,
  })

  // console.log(`Got ${res.status} ${res.statusText}`)
  if (res.ok) {
    return await res.text()
  }
}

// console.log('Starting')
// fetchJson().then((res) => {
//   console.log(`Got ${res.items.length} items`)
//   console.log(`${res.items[0].properties.content[0]}`)
// })

export {forward, injectBrowser, poll}
