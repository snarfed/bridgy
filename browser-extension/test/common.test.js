'use strict'

import fetchMock from 'jest-fetch-mock'

import {
  BRIDGY_BASE_URL,
  INDIEAUTH_START,
  injectGlobals,
  login,
  Silo,
} from '../common.js'

fetchMock.enableMocks()

beforeAll(() => {
  injectGlobals({
    // browser is a namespace, so we can't use jest.mock(), have to mock and inject
    // it manually like this.
    browser: {
      cookies: {
        getAll: jest.fn(),
        getAllCookieStores: async () => [{id: '1'}]
      },
      storage: {
        sync: {
          get: async () => browser.storage.sync.data,
          set: async values => Object.assign(browser.storage.sync.data, values),
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
  browser.cookies.getAll.mockResolvedValue([
    {name: 'seshun', value: 'foo'},
    {name: 'bar', value: 'baz'},
  ])
})

afterEach(() => {
  jest.restoreAllMocks()
})


class FakeSilo extends Silo {
  DOMAIN = 'fa.ke'
  BASE_URL = 'http://fa.ke'
  LOGIN_URL = 'http://fa.ke/login'
  COOKIE = 'seshun'
}


test('login, no existing token', async () => {
  expect(browser.storage.sync.data).toEqual({})

  await login()

  const token = browser.storage.sync.data.token
  expect(token).toBeDefined()

  expect(browser.tabs.create.mock.calls.length).toEqual(1)
  expect(browser.tabs.create.mock.calls[0]).toEqual([{
    url: INDIEAUTH_START + '?token=' + token,
  }])
})

test('login, existing token', async () => {
  browser.storage.sync.data = {token: 'foo'}
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
      headers: {
        'Cookie': 'seshun=foo; bar=baz',
        'User-Agent': navigator.userAgent,
      },
    },
  ])
  expect(fetch.mock.calls[1]).toEqual([
    `${BRIDGY_BASE_URL}/bridgy-path`,
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
