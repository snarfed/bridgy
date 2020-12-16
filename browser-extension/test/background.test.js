'use strict'

import fetchMock from 'jest-fetch-mock'

import {forward, injectBrowser, poll} from '../background.js'


fetchMock.enableMocks()

// browser is a namespace, so we can't use jest.mock(), have to mock and inject
// it manually like this.
let browser = {
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
}

beforeAll(() => {
  injectBrowser(browser)
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
  fetch.mockResponseOnce('bridgy resp')

  expect(await forward('/ig-path', '/br-path')).toBe('bridgy resp')

  expect(fetch.mock.calls.length).toBe(2)
  expect(fetch.mock.calls[0]).toEqual([
    'https://www.instagram.com/ig-path',
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
    'https://brid.gy/instagram/browser/br-path',
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
  fetch.mockResponseOnce('snarfed')
  fetch.mockResponseOnce('ig profile')
  fetch.mockResponseOnce('')

  await poll()
  expect(fetch.mock.calls.length).toBe(4)
  expect(fetch.mock.calls[0][0]).toBe('https://www.instagram.com/')
  expect(fetch.mock.calls[1][0]).toBe('https://brid.gy/instagram/browser/username')
  expect(fetch.mock.calls[2][0]).toBe('https://www.instagram.com/snarfed/')
  expect(fetch.mock.calls[3][0]).toBe('https://brid.gy/instagram/browser/profile')
  expect(fetch.mock.calls[3][1].body).toBe('ig profile')
  expect(await browser.storage.sync.get()).toEqual({instagram: {username: 'snarfed'}})
})

test('poll, existing username stored', async () => {
  await browser.storage.sync.set({instagram: {username: 'snarfed'}})
  await poll()
  expect(fetch.mock.calls.length).toBe(0)
})
