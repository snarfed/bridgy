'use strict'

import fetchMock from 'jest-fetch-mock'

import {fetchIG, injectBrowser} from '../background.js'


fetchMock.enableMocks()

// browser is a namespace, so we can't use jest.mock(), have to mock and inject
// it manually like this.
let browser = {
  cookies: {
    getAll: jest.fn(),
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


test('fetchIG', async () => {
  browser.cookies.getAll.mockResolvedValue([
    {name: 'foo', value: 'bar'},
    {name: 'baz', value: 'biff'},
  ])

  const body = 'some data'
  fetch.mockResponseOnce(body)

  expect(await fetchIG('/the-path')).toBe(body)
  expect(fetch.mock.calls[0][0]).toBe('https://www.instagram.com/the-path');
  expect(fetch.mock.calls[0][1]).toStrictEqual({
    method: 'GET',
    credentials: 'same-origin',
    headers: {
      'Cookie': 'foo=bar; baz=biff',
      'User-Agent': navigator.userAgent,
    },
  })
})
