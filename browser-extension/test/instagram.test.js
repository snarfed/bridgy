'use strict'

import {
  BRIDGY_BASE_URL,
} from '../common.js'

import {
  Instagram,
} from '../instagram.js'

import './testutil.js'

beforeEach(() => {
  browser.storage.local.data = {'instagram-bridgySourceKey': 'KEE'}
  browser.cookies.getAll.mockResolvedValue([{name: 'sessionid', value: 'foo'}])
})

const activities = [{
  id: '246',
  url: 'https://www.instagram.com/246',
  object: {
    ig_shortcode: 'abc',
    ig_like_count: 5,
    replies: {totalItems: 3}},
}, {
  id: '357',
  url: 'https://www.instagram.com/357',
  object: {
    ig_shortcode: 'xyz',
    ig_like_count: 0,
    replies: {totalItems: 0},
  },
}]


test('profilePath, homepage fetch for username', async () => {
  // no username stored
  expect(browser.storage.local.data['instagram-username']).toBeUndefined()

  fetch.mockResponseOnce('ig home page')
  fetch.mockResponseOnce('"snarfed"')

  expect(await Instagram.profilePath()).toBe('/snarfed/')
  expect(await browser.storage.local.get()).toMatchObject({
    'instagram-username': 'snarfed',
  })
})

test('profilePath, stored username', async () => {
  browser.storage.local.data = {'instagram-username': 'snarfed'}
  expect(await Instagram.profilePath()).toBe('/snarfed/')
})

test('poll, no stored username', async () => {
  // no username stored
  expect(browser.storage.local.data['instagram-username']).toBeUndefined()

  fetch.mockResponseOnce('ig home page')
  fetch.mockResponseOnce('"snarfed"')
  fetch.mockResponseOnce('ig profile')
  fetch.mockResponseOnce(JSON.stringify(activities))
  fetch.mockResponseOnce('post 246')
  fetch.mockResponseOnce('{"object": {"replies": {"items": [1,2]}}}')
  fetch.mockResponseOnce('reactions 246')
  fetch.mockResponseOnce('[1,2,3]')
  fetch.mockResponseOnce('post 357')
  fetch.mockResponseOnce('{}')
  fetch.mockResponseOnce('reactions 357')
  fetch.mockResponseOnce('[]')
  fetch.mockResponseOnce('"OK"')

  await Instagram.poll()
  expect(fetch.mock.calls.length).toBe(13)

  expect(fetch.mock.calls[0][0]).toBe('https://www.instagram.com/')
  expect(fetch.mock.calls[1][0]).toBe(
    `${BRIDGY_BASE_URL}/instagram/browser/homepage?token=towkin&key=KEE`)

  expect(await browser.storage.local.get()).toMatchObject({
    'instagram-username': 'snarfed',
    'instagram-post-246': {c: 2, r: 3},
    'instagram-post-357': {c: 0, r: 0},
  })

  expect(fetch.mock.calls[2][0]).toBe('https://www.instagram.com/snarfed/')
  expect(fetch.mock.calls[3][0]).toBe(
    `${BRIDGY_BASE_URL}/instagram/browser/feed?token=towkin&key=KEE`)
  expect(fetch.mock.calls[3][1].body).toBe('ig profile')

  for (const [i, shortcode, id] of [[4, 'abc', '246'], [8, 'xyz', '357']]) {
    expect(fetch.mock.calls[i][0]).toBe(`https://www.instagram.com/${id}`)
    expect(fetch.mock.calls[i + 1][0]).toBe(
      `${BRIDGY_BASE_URL}/instagram/browser/post?token=towkin&key=KEE`)
    expect(fetch.mock.calls[i + 1][1].body).toBe(`post ${id}`)
    expect(fetch.mock.calls[i + 2][0]).toContain('https://www.instagram.com/graphql/')
    expect(fetch.mock.calls[i + 2][0]).toContain(shortcode)
    expect(fetch.mock.calls[i + 3][0]).toBe(
      `${BRIDGY_BASE_URL}/instagram/browser/reactions?id=${id}&token=towkin&key=KEE`)
    expect(fetch.mock.calls[i + 3][1].body).toBe(`reactions ${id}`)
  }

  expect(fetch.mock.calls[12][0]).toBe(
    `${BRIDGY_BASE_URL}/instagram/browser/poll?token=towkin&key=KEE`)
})

test('poll, bridgy homepage error', async () => {
  // no username stored
  expect(browser.storage.local.data['instagram-username']).toBeUndefined()

  fetch.mockResponseOnce('ig home page')
  fetch.mockResponseOnce('{}', {status: 400})  // Bridgy returns an HTTP error
  await Instagram.poll()

  expect(fetch.mock.calls.length).toBe(2)
  expect(browser.storage.local.data['instagram-username']).toBeUndefined()
  expect(browser.storage.local.data['instagram-lastStart']).toBeDefined()
  expect(browser.storage.local.data['instagram-lastSuccess']).toBeUndefined()
})

test('poll, existing username stored', async () => {
  await browser.storage.local.set({'instagram-username': 'snarfed'})
  await Instagram.poll()
  expect(fetch.mock.calls[0][0]).toBe('https://www.instagram.com/snarfed/')
})

test('poll, feed error', async () => {
  fetch.mockResponseOnce('ig feed')
  fetch.mockResponseOnce('{}', {status: 400})  // Bridgy returns an HTTP error

  await browser.storage.local.set({'instagram-username': 'snarfed'})
  await Instagram.poll()

  expect(fetch.mock.calls.length).toBe(2)
  expect(browser.storage.local.data['instagram-lastStart']).toBeDefined()
  expect(browser.storage.local.data['instagram-lastSuccess']).toBeUndefined()
})

test('poll, Bridgy non-JSON response', async () => {
  fetch.mockResponseOnce('ig profile')
  fetch.mockResponseOnce('xyz')  // Bridgy returns invalid JSON

  await browser.storage.local.set({'instagram-username': 'snarfed'})
  await Instagram.poll()

  expect(fetch.mock.calls.length).toBe(2)
  expect(browser.storage.local.data['instagram-lastStart']).toBeDefined()
  expect(browser.storage.local.data['instagram-lastSuccess']).toBeUndefined()
})
