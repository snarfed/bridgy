'use strict'

import {
  BRIDGY_BASE_URL,
  INDIEAUTH_START,
  login,
  Silo,
} from '../common.js'

import './testutil.js'


beforeEach(() => {
  browser.storage.local.data = {'fake-bridgySourceKey': 'KEE'}
  browser.cookies.getAll.mockResolvedValue([
    {name: 'seshun', value: 'foo'},
    {name: 'bar', value: 'baz'},
  ])
})


class FakeSilo extends Silo {
  static async profilePath() {
    return '/profile'
  }

  static async feedPath() {
    return '/feed'
  }

  static reactionsCount(activity) {
    return activity.reactions_count
  }

  static reactionsPath(activity) {
    return `/reactions/${activity.id}`
  }
}

Silo.DOMAIN = 'fa.ke'
Silo.NAME = 'fake'
Silo.BASE_URL = 'http://fa.ke'
Silo.LOGIN_URL = 'http://fa.ke/login'
Silo.COOKIE = 'seshun'

const activities = [{
  id: '246',
  url: 'http://fa.ke/246',
  reactions_count: 5,
  object: {replies: {totalItems: 3}},
}, {
  id: '357',
  url: 'http://fa.ke/357',
  reactions_count: 0,
  object: {replies: {totalItems: 0}},
}]


test('login, no existing token', async () => {
  delete browser.storage.sync.data['token']
  await login()
  const token = browser.storage.sync.data.token
  expect(token).toBeDefined()

  expect(browser.tabs.create.mock.calls.length).toEqual(1)
  expect(browser.tabs.create.mock.calls[0]).toEqual([{
    url: INDIEAUTH_START + '?token=' + token,
  }])
})

test('login, existing token', async () => {
  await login()
  expect(browser.tabs.create.mock.calls.length).toEqual(0)
})

test('forward', async () => {
  fetch.mockResponseOnce('silo resp')
  fetch.mockResponseOnce('"bridgy resp"')

  expect(await FakeSilo.forward('/silo-path', '/bridgy-path')).toBe('bridgy resp')

  expect(fetch.mock.calls.length).toBe(2)
  expect(fetch.mock.calls[0]).toEqual([
    'http://fa.ke/silo-path',
    {
      method: 'GET',
      redirect: 'follow',
      headers: {'X-Bridgy': '1'},
    },
  ])
  expect(fetch.mock.calls[1]).toEqual([
    `${BRIDGY_BASE_URL}/fake/browser/bridgy-path?token=towkin&key=KEE`,
    {
      method: 'POST',
      body: 'silo resp',
    },
  ])
})

test('forward, no stored key', async () => {
  delete browser.storage.local.data['fake-bridgySourceKey']

  fetch.mockResponseOnce('silo resp')
  fetch.mockResponseOnce('"bridgy resp"')

  await FakeSilo.forward('/silo-path', '/bridgy-path')

  expect(fetch.mock.calls[1]).toEqual([
    `${BRIDGY_BASE_URL}/fake/browser/bridgy-path?token=towkin`,
    {
      method: 'POST',
      body: 'silo resp',
    },
  ])
})

test('forward, non-JSON response from Bridgy', async () => {
  fetch.mockResponseOnce('resp')
  fetch.mockResponseOnce('')  // not valid JSON
  expect(await FakeSilo.forward('/silo-path', '/bridgy-path')).toBeNull()
})

test('poll, no stored token', async () => {
  // no token stored
  browser.storage.sync.data = {}
  await FakeSilo.poll()

  expect(fetch.mock.calls.length).toBe(0)
  expect(browser.storage.local.data['fake-lastStart']).toBeUndefined()
  expect(browser.storage.local.data['fake-lastSuccess']).toBeUndefined()
  expect(browser.storage.local.data['fake-lastResponse']).toBeUndefined()
})

async function pollWithResponses() {
  fetch.mockResponseOnce('{}')
  fetch.mockResponseOnce('fake feed')
  fetch.mockResponseOnce(JSON.stringify(activities))
  fetch.mockResponseOnce('post 246')
  fetch.mockResponseOnce('{"object": {"replies": {"items": [1,2]}}}')
  fetch.mockResponseOnce('reactions 246')
  fetch.mockResponseOnce('[1, 2, 3, 4]')
  fetch.mockResponseOnce('post 357')
  fetch.mockResponseOnce('{"object": {}}')
  fetch.mockResponseOnce('reactions 357')
  fetch.mockResponseOnce('[]')
  fetch.mockResponseOnce('"OK"')

  await FakeSilo.poll()
  expect(fetch.mock.calls.length).toBe(12)
}

test('poll', async () => {
  const start = Date.now()
  await pollWithResponses()
  const end = Date.now()

  expect(browser.storage.local.data).toMatchObject({
    'fake-post-246': {c: 2, r: 4},
    'fake-post-357': {c: 0, r: 0},
  })

  for (const field of ['lastStart', 'lastSuccess', 'lastResponse']) {
    const timestamp = await FakeSilo.storageGet(field)
    expect(timestamp).toBeGreaterThanOrEqual(start)
    expect(timestamp).toBeLessThanOrEqual(end)
  }
  expect(await FakeSilo.storageGet('lastSuccess')).toBeGreaterThanOrEqual(
    await FakeSilo.storageGet('lastStart'))

  expect(fetch.mock.calls[1][0]).toBe('http://fa.ke/feed')
  expect(fetch.mock.calls[2][0]).toBe(
    `${BRIDGY_BASE_URL}/fake/browser/feed?token=towkin&key=KEE`)
  expect(fetch.mock.calls[2][1].body).toBe('fake feed')

  for (const [i, id] of [[3, '246'], [7, '357']]) {
    expect(fetch.mock.calls[i][0]).toBe(`http://fa.ke/${id}`)
    expect(fetch.mock.calls[i + 1][0]).toBe(
      `${BRIDGY_BASE_URL}/fake/browser/post?token=towkin&key=KEE`)
    expect(fetch.mock.calls[i + 1][1].body).toBe(`post ${id}`)
    expect(fetch.mock.calls[i + 2][0]).toBe(`http://fa.ke/reactions/${id}`)
    expect(fetch.mock.calls[i + 3][0]).toBe(
      `${BRIDGY_BASE_URL}/fake/browser/reactions?id=${id}&token=towkin&key=KEE`)
    expect(fetch.mock.calls[i + 3][1].body).toBe(`reactions ${id}`)
  }

  expect(fetch.mock.calls[11][0]).toBe(
    `${BRIDGY_BASE_URL}/fake/browser/poll?token=towkin&key=KEE`)
})

test('poll, status disabled', async () => {
  fetch.mockResponseOnce(JSON.stringify({
    status: 'disabled',
    'poll-seconds': 180,
  }))
  await FakeSilo.poll()

  expect(fetch.mock.calls.length).toBe(1)
  expect(browser.storage.local.data['fake-lastStart']).toBeUndefined()
  expect(browser.storage.local.data['fake-lastSuccess']).toBeUndefined()

  const alarm = await browser.alarms.get('bridgy-fake-poll')
  expect(alarm.delayInMinutes).toBe(3)
  expect(alarm.periodInMinutes).toBe(3)
})

test('poll, no stored token', async () => {
  // no token stored
  browser.storage.sync.data = {}
  await FakeSilo.poll()

  expect(fetch.mock.calls.length).toBe(0)
  expect(browser.storage.local.data['fake-lastStart']).toBeUndefined()
  expect(browser.storage.local.data['fake-lastSuccess']).toBeUndefined()
  expect(browser.storage.local.data['fake-lastResponse']).toBeUndefined()
  expect(browser.alarms.alarms).toEqual({})
})

test('poll, no stored bridgy source key', async () => {
  delete browser.storage.local.data['fake-bridgySourceKey']

  fetch.mockResponseOnce('{}')
  fetch.mockResponseOnce('fake profile')
  fetch.mockResponseOnce('"abc123"')

  await FakeSilo.poll()
  expect(fetch.mock.calls[1][0]).toBe('http://fa.ke/profile')
  expect(fetch.mock.calls[2][0]).toBe(
    `${BRIDGY_BASE_URL}/fake/browser/profile?token=towkin`)
  expect(browser.storage.local.data['fake-bridgySourceKey']).toBe('abc123')
  expect(browser.alarms.alarms).toEqual({})
})

test('poll, skip comments and reactions', async () => {
  fetch.mockResponseOnce('{}')
  fetch.mockResponseOnce('{}')
  fetch.mockResponseOnce(JSON.stringify(activities))
  fetch.mockResponseOnce('post 357')
  fetch.mockResponseOnce('{}')
  fetch.mockResponseOnce('reactions 357')
  fetch.mockResponseOnce('[]')
  fetch.mockResponseOnce('"OK"')

  await browser.storage.local.set({
    'fake-post-246': {c: 3, r: 5},
  })

  await FakeSilo.poll()
  expect(fetch.mock.calls.length).toBe(8)
  expect(fetch.mock.calls[1][0]).toBe('http://fa.ke/feed')
  expect(fetch.mock.calls[3][0]).toBe('http://fa.ke/357')

  expect(await browser.storage.local.get()).toMatchObject({
    'fake-post-246': {c: 3, r: 5},
    'fake-post-357': {c: 0, r: 0},
  })

  // this will be NaN if either value is undefined
  expect(browser.storage.local.data['fake-lastSuccess'] -
         browser.storage.local.data['fake-lastStart']).toBeLessThan(2000) // ms
  expect(browser.storage.local.data['fake-lastResponse']).toBeDefined()
})

test('poll, feed error', async () => {
  fetch.mockResponseOnce('{}')
  fetch.mockResponseOnce('fake feed')
  fetch.mockResponseOnce('{}', {status: 400})  // Bridgy returns an HTTP error
  await FakeSilo.poll()

  expect(fetch.mock.calls.length).toBe(3)
  expect(browser.storage.local.data['fake-lastStart']).toBeDefined()
  expect(browser.storage.local.data['fake-lastSuccess']).toBeUndefined()
  expect(browser.storage.local.data['fake-lastResponse']).toBeUndefined()
})

test('poll, Bridgy non-JSON response', async () => {
  fetch.mockResponseOnce('{}')
  fetch.mockResponseOnce('fake feed')
  fetch.mockResponseOnce('<html>xyz</html>')  // Bridgy returns invalid JSON
  await FakeSilo.poll()

  expect(fetch.mock.calls.length).toBe(3)
  expect(browser.storage.local.data['fake-lastStart']).toBeDefined()
  expect(browser.storage.local.data['fake-lastSuccess']).toBeUndefined()
})

test('poll, not enabled', async () => {
  await browser.storage.local.set({
    'fake-enabled': false,
  })
  await FakeSilo.poll()

  expect(fetch.mock.calls.length).toBe(0)
  expect(browser.storage.local.data['fake-lastStart']).toBeUndefined()
  expect(browser.storage.local.data['fake-lastSuccess']).toBeUndefined()
  expect(browser.storage.local.data['fake-lastResponse']).toBeUndefined()
})


async function pollNoActivities() {
  fetch.mockResponseOnce('{}')
  fetch.mockResponseOnce('fake feed')
  fetch.mockResponseOnce('[]')
  fetch.mockResponseOnce('"OK"')

  await FakeSilo.poll()
  expect(fetch.mock.calls.length).toBe(4)
}

async function pollNoResponses() {
  fetch.mockResponseOnce('{}')
  fetch.mockResponseOnce('fake feed')
  fetch.mockResponseOnce(JSON.stringify([activities[1]]))
  fetch.mockResponseOnce('post 357')
  fetch.mockResponseOnce('{}')
  fetch.mockResponseOnce('reactions 357')
  fetch.mockResponseOnce('[]')
  fetch.mockResponseOnce('"OK"')

  await FakeSilo.poll()
  expect(fetch.mock.calls.length).toBe(8)
}

test('poll, initial, no activities', async () => {
  await pollNoActivities()
  expect(browser.storage.local.data['fake-lastResponse']).toBeDefined()
})

test('poll, existing lastResponse, no activities', async () => {
  browser.storage.local.data['fake-lastResponse'] = 123
  await pollNoActivities()
  expect(browser.storage.local.data['fake-lastResponse']).toBe(123)
})

test('poll, initial, no comments or reactions', async () => {
  await pollNoResponses()
  expect(browser.storage.local.data['fake-lastResponse']).toBeDefined()
})

test('poll, existing lastResponse, no comments or reactions', async () => {
  browser.storage.local.data['fake-lastResponse'] = 123
  await pollNoResponses()
  expect(browser.storage.local.data['fake-lastResponse']).toBe(123)
})


test('poll, initial, with comments/reactions', async () => {
  await pollWithResponses()
  expect(browser.storage.local.data['fake-lastResponse']).toBeDefined()
})

test('poll, existing lastResponse, with comments/reactions', async () => {
  browser.storage.local.data['fake-lastResponse'] = 123
  const start = Date.now()
  await pollWithResponses()
  expect(browser.storage.local.data['fake-lastResponse']).toBeGreaterThanOrEqual(start)
})

