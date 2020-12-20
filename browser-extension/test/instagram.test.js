'use strict'

import fetchMock from 'jest-fetch-mock'

import {
  forward,
  poll,
  injectGlobals,
  INSTAGRAM_BASE_URL,
  BRIDGY_BASE_URL,
} from '../instagram.js'


fetchMock.enableMocks()

beforeAll(() => {
  injectGlobals({
    // browser is a namespace, so we can't use jest.mock(), have to mock and inject
    // it manually like this.
    browser: {
      cookies: {
        getAll: jest.fn(),
      },
      storage: {
        sync: {
          get: async () => browser.storage.sync.data,
          set: async values => Object.assign(browser.storage.sync.data, values),
          data: {},
        },
      },
    },
    console: {
      debug: () => null
    }
  })
})

beforeEach(() => {
  fetch.resetMocks()
})

afterEach(() => {
  jest.restoreAllMocks()
})


test('forward', async () => {
  browser.cookies.getAll.mockResolvedValue([
    {name: 'foo', value: 'bar'},
    {name: 'baz', value: 'biff'},
  ])

  fetch.mockResponseOnce('ig resp')
  fetch.mockResponseOnce('"bridgy resp"')

  expect(await forward('/ig-path', '/br-path')).toBe('bridgy resp')

  expect(fetch.mock.calls.length).toBe(2)
  expect(fetch.mock.calls[0]).toEqual([
    `${INSTAGRAM_BASE_URL}/ig-path`,
    {
      method: 'GET',
      credentials: 'same-origin',
      headers: {
        'Cookie': 'foo=bar; baz=biff',
        'User-Agent': navigator.userAgent,
      },
    },
  ])
  expect(fetch.mock.calls[1]).toEqual([
    `${BRIDGY_BASE_URL}/br-path`,
    {
      method: 'POST',
      body: 'ig resp',
    },
  ])
})

test('poll, no stored username', async () => {
  // no username stored
  expect(await browser.storage.sync.get()).toEqual({})

  fetch.mockResponseOnce('ig home page')
  fetch.mockResponseOnce('"snarfed"')
  fetch.mockResponseOnce('ig profile')
  fetch.mockResponseOnce(JSON.stringify([
    {
      id: '246',
      object: {ig_shortcode: 'abc'},
    }, {
      id: '357',
      object: {ig_shortcode: 'xyz'},
    }]))
  fetch.mockResponseOnce('post abc')
  fetch.mockResponseOnce('{}')
  fetch.mockResponseOnce('likes abc')
  fetch.mockResponseOnce('{}')
  fetch.mockResponseOnce('post xyz')
  fetch.mockResponseOnce('{}')
  fetch.mockResponseOnce('likes xyz')
  fetch.mockResponseOnce('{}')
  fetch.mockResponseOnce('"OK"')

  await poll()
  expect(fetch.mock.calls.length).toBe(13)

  expect(fetch.mock.calls[0][0]).toBe(`${INSTAGRAM_BASE_URL}/`)
  expect(fetch.mock.calls[1][0]).toBe(`${BRIDGY_BASE_URL}/homepage`)
  expect(await browser.storage.sync.get()).toEqual({instagram: {username: 'snarfed'}})

  expect(fetch.mock.calls[2][0]).toBe(`${INSTAGRAM_BASE_URL}/snarfed/`)
  expect(fetch.mock.calls[3][0]).toBe(`${BRIDGY_BASE_URL}/profile`)
  expect(fetch.mock.calls[3][1].body).toBe('ig profile')

  for (const [i, shortcode, id] of [[4, 'abc', '246'], [8, 'xyz', '357']]) {
    expect(fetch.mock.calls[i][0]).toBe(`${INSTAGRAM_BASE_URL}/p/${shortcode}/`)
    expect(fetch.mock.calls[i + 1][0]).toBe(`${BRIDGY_BASE_URL}/post`)
    expect(fetch.mock.calls[i + 1][1].body).toBe(`post ${shortcode}`)
    expect(fetch.mock.calls[i + 2][0]).toContain(`${INSTAGRAM_BASE_URL}/graphql/`)
    expect(fetch.mock.calls[i + 2][0]).toContain(shortcode)
    expect(fetch.mock.calls[i + 3][0]).toBe(`${BRIDGY_BASE_URL}/likes?id=${id}`)
    expect(fetch.mock.calls[i + 3][1].body).toBe(`likes ${shortcode}`)
  }

  expect(fetch.mock.calls[12][0]).toBe(`${BRIDGY_BASE_URL}/poll?username=snarfed`)
})

test('poll, existing username stored', async () => {
  await browser.storage.sync.set({instagram: {username: 'snarfed'}})
  await poll()
  expect(fetch.mock.calls[0][0]).toBe(`${INSTAGRAM_BASE_URL}/snarfed/`)
})
