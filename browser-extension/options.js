'use strict'

import './browser-polyfill.js'

import {login} from '../common.js'
import {findCookies, poll, LOGIN_URL} from './instagram.js'

function update() {
  console.debug('Updating options page fields')
  browser.storage.sync.get().then(data => {
    document.querySelector('#token').innerText = data.token

    if (data.instagramUsername) {
      document.querySelector('#username').innerText = data.instagramUsername
      document.querySelector('#username').href = `https://www.instagram.com/${data.instagramUsername}/`
      document.querySelector('#user-page').innerText = `brid.gy/instagram/${data.instagramUsername}`
      document.querySelector('#user-page').href = `https://brid.gy/instagram/${data.instagramUsername}`
    }

    if (data.instagramLastStart) {
      document.querySelector('#last-start').innerText = new Date(data.instagramLastStart).toLocaleString()
    }
    if (data.instagramLastSuccess) {
      document.querySelector('#last-success').innerText = new Date(data.instagramLastSuccess).toLocaleString()
    }

    const posts = Object.entries(data).filter(x => x[0].startsWith('instagramPost-'))
    const comments = posts.reduce((sum, cur) => sum + cur[1].c, 0)
    const likes = posts.reduce((sum, cur) => sum + cur[1].l, 0)
    document.querySelector('#posts').innerText = posts.length
    document.querySelector('#comments').innerText = comments
    document.querySelector('#likes').innerText = likes

    findCookies().then((cookies) => updateStatus(data, cookies))
  })
}

function updateStatus(data, cookies) {
    // Link to console logs for reporting bugs:
    // about:devtools-toolbox?type=extension&id=bridgy2%40snarfed.org
    let status = document.querySelector('#status')
    if (!cookies) {
      status.innerHTML = `No Instagram cookie found. <a href="${LOGIN_URL}">Try logging in!</a>`
      status.className = 'error'
    } else if (!data.instagramLastStart) {
      status.innerText = 'Not started yet'
      status.className = 'pending'
    } else if (!data.instagramLastSuccess) {
      status.innerText = 'Poll is failing'
      status.className = 'error'
    } else if (data.instagramLastStart > data.instagramLastSuccess) {
      status.innerText = 'Poll was working but is now failing'
      status.className = 'error'
    } else if (data.instagramLastSuccess > data.instagramLastStart) {
      status.innerText = 'OK!'
      status.className = 'ok'
    }
}

document.addEventListener('DOMContentLoaded', function () {
  document.querySelector('#poll').addEventListener('click', () => poll().then(update))
  document.querySelector('#reconnect').addEventListener('click', () => login(true))

  console.debug('Scheduling options page refresh every minute')
  browser.alarms.onAlarm.addListener((alarm) => {
    if (alarm.name == 'bridgy-options-page-refresh') {
      update()
    }
  })
  browser.alarms.create('bridgy-options-page-refresh', {
    periodInMinutes: 1,
  })
  update()

  // oddly, this doesn't work. the options page's visibilityState is always
  // 'visible' when it exists. hrm.
  // document.addEventListener('visibilitychange', function() {
  //   if (document.visibilityState === 'visible') {
  //     update()
  //   }
  // })
})

