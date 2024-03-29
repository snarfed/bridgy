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

  static headers() {
    return {'foo': 'bar'}
  }
}

class FakeReactionsSilo extends FakeSilo {
  static reactionsPath(activity) {
    return `/reactions/${activity.id}`
  }
}

class FakeEverythingSilo extends FakeReactionsSilo {
  static commentsPath(activity) {
    return `https://sub.fa.ke/comments/${activity.id}`
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

  expect(await FakeReactionsSilo.forward('/silo-path', '/bridgy-path')).toBe('bridgy resp')

  expect(fetch.mock.calls.length).toBe(2)
  expect(fetch.mock.calls[0]).toEqual([
    'http://fa.ke/silo-path',
    {
      method: 'GET',
      redirect: 'follow',
      headers: {'X-Bridgy': '1', 'foo': 'bar'},
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

  await FakeReactionsSilo.forward('/silo-path', '/bridgy-path')

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
  expect(await FakeReactionsSilo.forward('/silo-path', '/bridgy-path')).toBeNull()
})

test('poll, no stored token', async () => {
  // no token stored
  browser.storage.sync.data = {}
  await FakeReactionsSilo.poll()

  expect(fetch.mock.calls.length).toBe(0)
  expect(browser.storage.local.data['fake-lastStart']).toBeUndefined()
  expect(browser.storage.local.data['fake-lastSuccess']).toBeUndefined()
  expect(browser.storage.local.data['fake-lastResponse']).toBeUndefined()
})

async function pollWithResponses(cls) {
  fetch.mockResponseOnce('{}')
  fetch.mockResponseOnce('fake feed')
  fetch.mockResponseOnce(JSON.stringify(activities))
  fetch.mockResponseOnce('post 246')

  if (cls == FakeEverythingSilo) {
    fetch.mockResponseOnce('{"id": "tag:instagram.com:123", "object": {}}')
    fetch.mockResponseOnce('fake comments')
    fetch.mockResponseOnce('[1, 2]')
  } else {
    fetch.mockResponseOnce('{"object": {"replies": {"items": [1, 2]}}}')
  }

  if (cls != FakeSilo) {
    fetch.mockResponseOnce('reactions 246')
    fetch.mockResponseOnce('[1, 2, 3, 4]')
  }

  fetch.mockResponseOnce('post 357')
  fetch.mockResponseOnce('{"id": "tag:instagram.com:456", "object": {"replies": {"totalItems": 0}}}')

  if (cls != FakeSilo) {
    fetch.mockResponseOnce('reactions 357')
    fetch.mockResponseOnce('[]')
  }
  fetch.mockResponseOnce('"OK"')

  await cls.poll()
  expect(fetch.mock.calls.length).toBe(
    cls == FakeSilo ? 8 : (cls == FakeReactionsSilo ? 12 : 16))
}

async function checkTimestamps(start, end) {
  for (const field of ['lastStart', 'lastSuccess', 'lastResponse']) {
    const timestamp = await FakeReactionsSilo.storageGet(field)
    expect(timestamp).toBeGreaterThanOrEqual(start)
    expect(timestamp).toBeLessThanOrEqual(end)
  }
  expect(await FakeReactionsSilo.storageGet('lastSuccess')).toBeGreaterThanOrEqual(
    await FakeReactionsSilo.storageGet('lastStart'))
}

function checkPollFetches() {
  expect(fetch.mock.calls[1][0]).toBe('http://fa.ke/feed')
  expect(fetch.mock.calls[2][0]).toBe(
    `${BRIDGY_BASE_URL}/fake/browser/feed?token=towkin&key=KEE`)
  expect(fetch.mock.calls[2][1].body).toBe('fake feed')

  expect(fetch.mock.calls[fetch.mock.calls.length - 1][0]).toBe(
    `${BRIDGY_BASE_URL}/fake/browser/poll?token=towkin&key=KEE`)
}

test('poll foo', async () => {
  const start = Date.now()
  await pollWithResponses(FakeSilo)

  expect(browser.storage.local.data).toMatchObject({
    'fake-post-246': {c: 2, r: 0},
    'fake-post-357': {c: 0, r: 0},
  })

  await checkTimestamps(start, Date.now())
  checkPollFetches()
  for (const [i, id] of [[3, '246'], [5, '357']]) {
    expect(fetch.mock.calls[i][0]).toBe(`http://fa.ke/${id}`)
    expect(fetch.mock.calls[i + 1][0]).toBe(
      `${BRIDGY_BASE_URL}/fake/browser/post?token=towkin&key=KEE`)
    expect(fetch.mock.calls[i + 1][1].body).toBe(`post ${id}`)
  }
})

test('poll, with reactions', async () => {
  const start = Date.now()
  await pollWithResponses(FakeReactionsSilo)

  expect(browser.storage.local.data).toMatchObject({
    'fake-post-246': {c: 2, r: 4},
    'fake-post-357': {c: 0, r: 0},
  })

  await checkTimestamps(start, Date.now())

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
})

test('poll, status disabled', async () => {
  fetch.mockResponseOnce(JSON.stringify({
    status: 'disabled',
    'poll-seconds': 180,
  }))
  await FakeReactionsSilo.poll()

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
  await FakeReactionsSilo.poll()

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

  await FakeReactionsSilo.poll()
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

  await FakeReactionsSilo.poll()
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
  fetch.mockResponseOnce('air-roar', {status: 400})  // Bridgy returns an HTTP error
  await FakeReactionsSilo.poll()

  expect(fetch.mock.calls.length).toBe(3)
  expect(browser.storage.local.data['fake-lastStart']).toBeDefined()
  expect(browser.storage.local.data['fake-lastSuccess']).toBeUndefined()
  expect(browser.storage.local.data['fake-lastResponse']).toBeUndefined()
  expect(browser.storage.local.data['fake-lastError']).toEqual('air-roar')
})

test('poll, Bridgy non-JSON response', async () => {
  fetch.mockResponseOnce('{}')
  fetch.mockResponseOnce('fake feed')
  fetch.mockResponseOnce('<html>xyz</html>')  // Bridgy returns invalid JSON
  await FakeReactionsSilo.poll()

  expect(fetch.mock.calls.length).toBe(3)
  expect(browser.storage.local.data['fake-lastStart']).toBeDefined()
  expect(browser.storage.local.data['fake-lastSuccess']).toBeUndefined()
})

test('poll, not enabled', async () => {
  await browser.storage.local.set({
    'fake-enabled': false,
  })
  await FakeReactionsSilo.poll()

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

  await FakeReactionsSilo.poll()
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

  await FakeReactionsSilo.poll()
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
  await pollWithResponses(FakeReactionsSilo)
  expect(browser.storage.local.data['fake-lastResponse']).toBeDefined()
})


test('poll, initial, comments fetch', async () => {
  await pollWithResponses(FakeEverythingSilo)
  expect(browser.storage.local.data['fake-lastResponse']).toBeDefined()
})

test('poll, existing lastResponse, with comments/reactions', async () => {
  browser.storage.local.data['fake-lastResponse'] = 123
  browser.storage.local.data['fake-lastError'] = 'foo'
  const start = Date.now()
  await pollWithResponses(FakeReactionsSilo)
  expect(browser.storage.local.data['fake-lastResponse']).toBeGreaterThanOrEqual(start)
  expect(browser.storage.local.data['fake-lastError']).toBeNull()
})
