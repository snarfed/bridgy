'use strict'

/**
 * Makes an HTTP GET request and returns the JSON response.
 */
async function fetchJson() {
  const url = 'https://granary.io/instagram/snarfed/@self/@app/?format=mf2-json&cookie=...&interactive=true'
  console.log(`Fetching ${url}`)
  const res = await fetch(url, {method: 'GET'})
  if (!res.ok) {
    console.log(`Granary error: ${res} ${res.status} ${res.statusText}`)
    return null
  }
  return await res.json()
}

// console.log('Starting')
// fetchJson().then((res) => {
//   console.log(`Got ${res.items.length} items`)
//   console.log(`${res.items[0].properties.content[0]}`)
// })

export { fetchJson }
