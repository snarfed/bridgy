'use strict'

/**
 * Injects a mock browser namespace for tests.
 */
function injectBrowser(browser) {
  global.browser = browser
}

/**
 * Makes an HTTP GET request to Instagram and returns the response body as a string.
 */
async function fetchIG(path) {
  // TODO: fetch cookies from all stores, not just the default one, in order to
  // support containers.
  // https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/Work_with_the_Cookies_API#Cookie_stores
  // https://hacks.mozilla.org/2017/10/containers-for-add-on-developers/
  // https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/API/cookies/getAll
  const cookies = await browser.cookies.getAll({domain: 'instagram.com'})
  const url = 'https://www.instagram.com' + path
  // console.log(`Fetching ${url}`)

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

  if (!res.ok) {
    console.log(`Granary error: ${res} ${res.status} ${res.statusText}`)
    return null
  }
  return await res.text()
}

// console.log('Starting')
// fetchJson().then((res) => {
//   console.log(`Got ${res.items.length} items`)
//   console.log(`${res.items[0].properties.content[0]}`)
// })

export { fetchIG, injectBrowser }
