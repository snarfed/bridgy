'use strict'

import fetchMock from 'jest-fetch-mock'

import {
  BRIDGY_BASE_URL,
  INDIEAUTH_START,
  injectGlobals,
  login,
  Silo,
} from '../common.js'


const postActivities = [{
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

class FakeSilo extends Silo {
  DOMAIN = 'fa.ke'
  NAME = 'fake'
  BASE_URL = 'http://fa.ke'
  LOGIN_URL = 'http://fa.ke/login'
  COOKIE = 'seshun'

  async profilePath() {
    return '/profile'
  }

  async feedPath() {
    return '/feed'
  }

  reactionsCount(activity) {
    return activity.reactions_count
  }

  reactionsPath(activity) {
    return `/reactions/${activity.id}`
  }
}


fetchMock.enableMocks()

beforeAll(() => {
  injectGlobals({
    // browser is a namespace, so we can't use jest.mock(), have to mock and inject
    // it manually like this.
    browser: {
      cookies: {
        getAll: jest.fn(),
        getAllCookieStores: async () => [{id: '1'}],
      },
      storage: {
        sync: {
          get: async function () { return this.data },
          set: async function (values) { Object.assign(this.data, values) },
          data: {},
        },
        local: {
          get: async function () { return this.data },
          set: async function (values) { Object.assign(this.data, values) },
          data: {},
        },
      },
      tabs: {
        create: jest.fn(),
      },
    },
    console: {
      debug: () => null,
      log: () => null,
      error: () => null,
    },
    _console: console,
  })
})

beforeEach(() => {
  jest.resetAllMocks()
  fetch.resetMocks()

  browser.storage.sync.data = {'token': 'towkin'}
  browser.storage.local.data = {'fake-bridgySourceKey': 'KEE'}
  browser.cookies.getAll.mockResolvedValue([
    {name: 'seshun', value: 'foo'},
    {name: 'bar', value: 'baz'},
  ])
})

afterEach(() => {
  jest.restoreAllMocks()
})


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

  let fake = new FakeSilo()
  expect(await fake.forward('/silo-path', '/bridgy-path')).toBe('bridgy resp')

  expect(fetch.mock.calls.length).toBe(2)
  expect(fetch.mock.calls[0]).toEqual([
    'http://fa.ke/silo-path',
    {
      method: 'GET',
      credentials: 'same-origin',
      redirect: 'follow',
      headers: {
        'Cookie': 'seshun=foo; bar=baz',
        'User-Agent': navigator.userAgent,
      },
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

  let fake = new FakeSilo()
  await fake.forward('/silo-path', '/bridgy-path')

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
  expect(await new FakeSilo().forward('/silo-path', '/bridgy-path')).toBeNull()
})

test('poll, no stored token', async () => {
  // no token stored
  browser.storage.sync.data = {}
  await new FakeSilo().poll()

  expect(fetch.mock.calls.length).toBe(0)
  expect(browser.storage.local.data['fake-lastStart']).toBeUndefined()
  expect(browser.storage.local.data['fake-lastSuccess']).toBeUndefined()
})

test('poll', async () => {
  fetch.mockResponseOnce('fake feed')
  fetch.mockResponseOnce(JSON.stringify(postActivities))
  fetch.mockResponseOnce('post 246')
  fetch.mockResponseOnce('{}')
  fetch.mockResponseOnce('reactions 246')
  fetch.mockResponseOnce('{}')
  fetch.mockResponseOnce('post 357')
  fetch.mockResponseOnce('{}')
  fetch.mockResponseOnce('reactions 357')
  fetch.mockResponseOnce('{}')
  fetch.mockResponseOnce('"OK"')

  await new FakeSilo().poll()
  expect(fetch.mock.calls.length).toBe(11)

  expect(browser.storage.local.data).toMatchObject({
    'fake-post-246': {c: 3, r: 5},
    'fake-post-357': {c: 0, r: 0},
  })
  // this will be NaN if either value is undefined
  expect(browser.storage.local.data['fake-lastSuccess'] -
         browser.storage.local.data['fake-lastStart']).toBeLessThan(2000) // ms

  expect(fetch.mock.calls[0][0]).toBe('http://fa.ke/feed')
  expect(fetch.mock.calls[1][0]).toBe(
    `${BRIDGY_BASE_URL}/fake/browser/feed?token=towkin&key=KEE`)
  expect(fetch.mock.calls[1][1].body).toBe('fake feed')

  for (const [i, id] of [[2, '246'], [6, '357']]) {
    expect(fetch.mock.calls[i][0]).toBe(`http://fa.ke/${id}`)
    expect(fetch.mock.calls[i + 1][0]).toBe(
      `${BRIDGY_BASE_URL}/fake/browser/post?token=towkin&key=KEE`)
    expect(fetch.mock.calls[i + 1][1].body).toBe(`post ${id}`)
    expect(fetch.mock.calls[i + 2][0]).toBe(`http://fa.ke/reactions/${id}`)
    expect(fetch.mock.calls[i + 3][0]).toBe(
      `${BRIDGY_BASE_URL}/fake/browser/reactions?id=${id}&token=towkin&key=KEE`)
    expect(fetch.mock.calls[i + 3][1].body).toBe(`reactions ${id}`)
  }

  expect(fetch.mock.calls[10][0]).toBe(
    `${BRIDGY_BASE_URL}/fake/browser/poll?token=towkin&key=KEE`)
})

test('poll, skip comments and likes', async () => {
  fetch.mockResponseOnce('{}')
  fetch.mockResponseOnce(JSON.stringify(postActivities))
  fetch.mockResponseOnce('post 357')
  fetch.mockResponseOnce('{}')
  fetch.mockResponseOnce('likes 357')
  fetch.mockResponseOnce('{}')
  fetch.mockResponseOnce('{}')

  await browser.storage.local.set({
    'fake-post-246': {c: 3, r: 5},
  })

  await new FakeSilo().poll()
  expect(fetch.mock.calls.length).toBe(7)
  expect(fetch.mock.calls[0][0]).toBe('http://fa.ke/feed')
  expect(fetch.mock.calls[2][0]).toBe('http://fa.ke/357')

  expect(await browser.storage.local.get()).toMatchObject({
    'fake-post-246': {c: 3, r: 5},
    'fake-post-357': {c: 0, r: 0},
  })

  // this will be NaN if either value is undefined
  expect(browser.storage.local.data['fake-lastSuccess'] -
         browser.storage.local.data['fake-lastStart']).toBeLessThan(2000) // ms
})

test('poll, feed error', async () => {
  fetch.mockResponseOnce('fake feed')
  fetch.mockResponseOnce('{}', {status: 400})  // Bridgy returns an HTTP error
  await new FakeSilo().poll()

  expect(fetch.mock.calls.length).toBe(2)
  expect(browser.storage.local.data['fake-lastStart']).toBeDefined()
  expect(browser.storage.local.data['fake-lastSuccess']).toBeUndefined()
})

test('poll, Bridgy non-JSON response', async () => {
  fetch.mockResponseOnce('fake feed')
  fetch.mockResponseOnce('<html>xyz</html>')  // Bridgy returns invalid JSON
  await new FakeSilo().poll()

  expect(fetch.mock.calls.length).toBe(2)
  expect(browser.storage.local.data['fake-lastStart']).toBeDefined()
  expect(browser.storage.local.data['fake-lastSuccess']).toBeUndefined()
})
