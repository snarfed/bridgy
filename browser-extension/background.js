import {poll} from './instagram.js'

const FREQUENCY_MIN = 30

function doPoll(alarm) {
  console.log('Starting poll...')
  poll().then(() => console.log('Done!'))
}

console.log(`Scheduling poll every ${FREQUENCY_MIN}m`)
browser.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name == 'bridgy-instagram-poll') {
    doPoll()
  }
})
browser.alarms.create('bridgy-instagram-poll', {
  delayInMinutes: 0,
  periodInMinutes: FREQUENCY_MIN,
})

doPoll()
