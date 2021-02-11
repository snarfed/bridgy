import './browser-polyfill.js'
import {login} from './common.js'
import {findCookies, poll, INSTAGRAM_LOGIN_URL} from './instagram.js'

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

function schedulePoll() {
  console.log(`Scheduling poll every ${FREQUENCY_MIN}m`)
  browser.alarms.onAlarm.addListener((alarm) => {
    if (alarm.name == 'bridgy-instagram-poll') {
      poll()
    }
  })
  browser.alarms.create('bridgy-instagram-poll', {
    delayInMinutes: 5,
    periodInMinutes: FREQUENCY_MIN,
  })
}

findCookies().then((cookies) => {
  if (!cookies) {
    browser.tabs.create({url: INSTAGRAM_LOGIN_URL})
  }
})

login().then(() => {
  schedulePoll()
})
