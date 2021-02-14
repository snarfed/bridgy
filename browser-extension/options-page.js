'use strict'

import './browser-polyfill.js'

import {login} from './common.js'
import {Facebook} from './facebook.js'
import {Instagram} from './instagram.js'
import {
  pollNow,
  update,
} from './options.js'

document.addEventListener('DOMContentLoaded', function () {
  document.querySelector('#reconnect').addEventListener('click', () => login(true))

  for (const silo of [new Instagram(), new Facebook()]) {
    document.querySelector(`#${silo.NAME}-poll`).addEventListener(
      'click', () => pollNow(silo))
  }

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
