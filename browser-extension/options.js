'use strict'

import './browser-polyfill.js'

import {login} from '../common.js'
import {poll} from './instagram.js'

function update() {
  browser.storage.sync.get().then(data => {
    document.querySelector('#token').innerText = data.token

    if (data.instagramUsername) {
      document.querySelector('#username').innerText = data.instagramUsername
      document.querySelector('#username').href = `https://www.instagram.com/${data.instagramUsername}/`
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
  })
}

document.addEventListener('DOMContentLoaded', function () {
  document.querySelector('#poll').addEventListener('click', () => poll().then(update))
  document.querySelector('#reconnect').addEventListener('click', () => login(true))
  update()
})
