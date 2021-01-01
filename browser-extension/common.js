'use strict'

const INDIEAUTH_START = 'https://brid.gy/indieauth/start'

/**
 * Injects mock globals for tests.
 */
function injectGlobals(newGlobals) {
  Object.assign(global, newGlobals)
}


/*
 * Initial setup: generate a token, then start IndieAuth flow on Bridgy to log
 * into their web site and connect that token.
 */
async function login() {
  const data = await browser.storage.sync.get(['token'])
  if (data.token) {
    return
  }

  const token = Math.random().toString(36).substring(2, 15)
  await browser.storage.sync.set({token: token})
  console.log(`Generated new token: ${token}. Starting IndieAuth flow.`)

  await browser.tabs.create({url: `${INDIEAUTH_START}?token=${token}`})
}

export {injectGlobals, login, INDIEAUTH_START}
