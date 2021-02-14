import './browser-polyfill.js'

import {login} from './common.js'
import {Instagram} from './instagram.js'
import {Facebook} from './facebook.js'

/* Local storage schema for this extension:
 *
 * browser.storage.sync:
 *   token: [string]
 *
 * browser.storage.local:
 *   [silo]-bridgySourceKey: [string],
 *   [silo]-lastStart: [Date],
 *   [silo]-lastSuccess: [Date],
 *   [silo]-post-[id]: {
 *     c: [integer],  // number of commenst
 *     r: [integer],  // number of reactions
 *   },
 */

const FREQUENCY_MIN = 30

let facebook = new Facebook()
let instagram = new Instagram()

function schedulePoll() {
  console.log(`Scheduling poll every ${FREQUENCY_MIN}m`)
  browser.alarms.onAlarm.addListener((alarm) => {
    if (alarm.name == 'bridgy-facebook-poll') {
      facebook.poll()
    } else if (alarm.name == 'bridgy-instagram-poll') {
      instagram.poll()
    }
  })

  for (const silo of [instagram, facebook]) {
    browser.alarms.create(`bridgy-${silo.NAME}-poll`, {
      delayInMinutes: 5,
      periodInMinutes: FREQUENCY_MIN,
    })
  }
}

for (const silo of [instagram, facebook]) {
  silo.findCookies().then((cookies) => {
    if (!cookies) {
      browser.tabs.create({url: silo.LOGIN_URL})
    }
  })
}

login().then(() => {
  schedulePoll()
})
