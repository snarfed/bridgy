'use strict'

import './browser-polyfill.js'

import {pollNow, update} from './options.js'
import {poll} from './instagram.js'
import {login} from './common.js'

document.addEventListener('DOMContentLoaded', function () {
  document.querySelector('#poll').addEventListener('click', () => pollNow())
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
