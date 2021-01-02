'use strict'

import {login} from '../common.js'

document.addEventListener('DOMContentLoaded', function () {
  document.querySelector('#reconnect').addEventListener('click', () => login(true))
  browser.storage.sync.get().then(data => {
    document.querySelector('#username').innerText = data.instagramUsername
    document.querySelector('#username').href = `https://www.instagram.com/${data.instagramUsername}/`
    document.querySelector('#token').innerText = data.token

    const posts = Object.entries(data).filter(x => x[0].startsWith('instagramPost-'))
    const comments = posts.reduce((sum, cur) => sum + cur[1].c, 0)
    const likes = posts.reduce((sum, cur) => sum + cur[1].l, 0)
    document.querySelector('#posts').innerText = posts.length
    document.querySelector('#comments').innerText = comments
    document.querySelector('#likes').innerText = likes
  })
})
