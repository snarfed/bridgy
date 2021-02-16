'use strict'

import './browser-polyfill.js'

import {
  BRIDGY_BASE_URL,
  INDIEAUTH_START,
} from './common.js'

import {Facebook} from './facebook.js'
import {Instagram} from './instagram.js'


async function update() {
  console.debug('Updating options page UI')

  const token = (await browser.storage.sync.get()).token
  var domains
  if (token) {
    document.querySelector('#token').innerText = token
    domains = await new Instagram().postBridgy(`/token-domains?token=${token}`)
    document.querySelector('#domains').innerText = (domains ? domains.join(', ') : 'none')
  }

  const data = await browser.storage.local.get()
  for (const silo of [new Instagram(), new Facebook()]) {
    const posts = Object.entries(data).filter(x => x[0].startsWith(`${silo.NAME}-post-`))
    const comments = posts.reduce((sum, cur) => sum + (cur[1].c || 0), 0)
    const reactions = posts.reduce((sum, cur) => sum + (cur[1].r || 0), 0)
    document.querySelector(`#${silo.NAME}-posts`).innerText = posts.length
    document.querySelector(`#${silo.NAME}-comments`).innerText = comments
    document.querySelector(`#${silo.NAME}-reactions`).innerText = reactions

    if (domains) {
      const userUrl = `${BRIDGY_BASE_URL}/${silo.NAME}/${domains[0]}`
      document.querySelector(`#${silo.NAME}-userPage`).innerText = userUrl.split('//')[1]
      document.querySelector(`#${silo.NAME}-userPage`).href = userUrl
    }

    const lastStart = data[`${silo.NAME}-lastStart`]
    if (lastStart) {
      const date = new Date(lastStart).toLocaleString()
      document.getElementById(`${silo.NAME}-lastStart`).innerText = date
    }

    const lastSuccess = data[`${silo.NAME}-lastSuccess`]
    if (lastSuccess) {
      const date = new Date(lastSuccess).toLocaleString()
      document.getElementById(`${silo.NAME}-lastSuccess`).innerText = date
    }

    const cookies = await silo.findCookies()
    let status = document.querySelector(`#${silo.NAME}-status`)
    if (!cookies) {
      status.innerHTML = `No ${silo.DOMAIN} cookie found. <a href="${silo.LOGIN_URL}" target="_blank">Try logging in!</a>`
      status.className = 'error'
    } else if (!domains) {
      status.innerHTML = `Not connected to Bridgy. <a href="${INDIEAUTH_START}?token=${token} target="_blank"">Connect now!</a>`
      status.className = 'error'
    } else if (!lastStart) {
      status.innerHTML = 'Not started yet'
      status.className = 'pending'
    } else if (!lastSuccess) {
      status.innerHTML = 'Initial poll did not succeed'
      status.className = 'error'
    } else if (lastSuccess >= lastStart) {
      status.innerHTML = 'OK'
      status.className = 'ok'
    } else if (lastStart > Date.now() - 30 * 1000) {
      status.innerHTML = 'Polling now...'
      status.className = 'pending'
    } else if (lastStart > lastSuccess) {
      status.innerHTML = 'Last poll did not succeed'
      // want to include this but can't get it to work. Firefox says
      // "Uncaught SyntaxError: private fields are not currently supported"
      // '<a href="#" onclick="document.querySelector('#poll').click()">Retry now!</a>
      status.className = 'error'
    }
  }

  const igUsername = data['instagram-username']
  if (igUsername) {
    document.querySelector('#instagram-username').innerText = igUsername
    document.querySelector('#instagram-username').href = `https://www.instagram.com/${igUsername}/`
  }
}

function pollNow(silo) {
  let status = document.querySelector(`#${silo.NAME}-status`)
  status.innerHTML = 'Polling now...'
  status.className = 'pending'
  silo.poll().then(update)
}

export {
  pollNow,
  update,
}
