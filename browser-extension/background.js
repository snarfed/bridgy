import './browser-polyfill.js'
import {login} from '../common.js'
import {poll} from './instagram.js'

/* Local storage schema for this extension:
 *
 * token: [string],
 * instagramUsername: [string],
 * instagramLastStart: [Date],
 * instagramLastSuccess: [Date],
 * 'instagramPost-[shortcode]': {
 *   c: [integer],  // number of commenst
 *   l: [integer],  // number of likes
 * },
 *
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

login().then(() => {
  schedulePoll()
})
