'use strict'

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

export {injectGlobals, login, INDIEAUTH_START}
