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
  id: 'tag:ig:246',
  url: 'https://www.instagram.com/246',
  object: {
    ig_shortcode: 'abc',
    ig_like_count: 5,
    replies: {totalItems: 3},
  },
}, {
  id: 'tag:ig:357',
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

  expect(await Instagram.profilePath()).toBe(
    '/api/v1/users/web_profile_info/?username=snarfed')
  expect(await browser.storage.local.get()).toMatchObject({
    'instagram-username': 'snarfed',
  })
})

test('profilePath, stored username', async () => {
  browser.storage.local.data = {'instagram-username': 'snarfed'}
  expect(await Instagram.profilePath()).toBe(
    '/api/v1/users/web_profile_info/?username=snarfed')
})

test('poll, no stored username', async () => {
  // no username stored
  expect(browser.storage.local.data['instagram-username']).toBeUndefined()

  fetch.mockResponseOnce('{}')
  fetch.mockResponseOnce('ig home page')
  fetch.mockResponseOnce('"snarfed"')
  fetch.mockResponseOnce('ig profile')
  fetch.mockResponseOnce(JSON.stringify(activities))
  fetch.mockResponseOnce('post 246')
  fetch.mockResponseOnce(JSON.stringify({
    object: {
      replies: {items: [1,2]},
      tags: [
        {verb: 'like', id: '7'},
        {verb: 'like', id: '8'},
        {verb: 'like', id: '9'},
      ],
    }}))
  fetch.mockResponseOnce('post 357')
  fetch.mockResponseOnce('{}')
  fetch.mockResponseOnce('"OK"')

  await Instagram.poll()
  expect(fetch.mock.calls.length).toBe(10)

  expect(fetch.mock.calls[1][0]).toBe('https://www.instagram.com/')
  expect(fetch.mock.calls[2][0]).toBe(
    `${BRIDGY_BASE_URL}/instagram/browser/homepage?token=towkin&key=KEE`)

  expect(await browser.storage.local.get()).toMatchObject({
    'instagram-username': 'snarfed',
    'instagram-post-tag:ig:246': {c: 2, r: 3},
    'instagram-post-tag:ig:357': {c: 0, r: 0},
  })

  expect(fetch.mock.calls[3][0]).toBe(
    'https://i.instagram.com/api/v1/users/web_profile_info/?username=snarfed')
  expect(fetch.mock.calls[4][0]).toBe(
    `${BRIDGY_BASE_URL}/instagram/browser/feed?token=towkin&key=KEE`)
  expect(fetch.mock.calls[4][1].body).toBe('ig profile')

  for (const [i, shortcode, id] of [[5, 'abc', '246'], [7, 'xyz', '357']]) {
    expect(fetch.mock.calls[i][0]).toBe(`https://www.instagram.com/${id}`)
    expect(fetch.mock.calls[i + 1][0]).toBe(
      `${BRIDGY_BASE_URL}/instagram/browser/post?token=towkin&key=KEE`)
    expect(fetch.mock.calls[i + 1][1].body).toBe(`post ${id}`)
  }

  expect(fetch.mock.calls[9][0]).toBe(
    `${BRIDGY_BASE_URL}/instagram/browser/poll?token=towkin&key=KEE`)
})

test('poll, bridgy homepage error', async () => {
  // no username stored
  expect(browser.storage.local.data['instagram-username']).toBeUndefined()

  fetch.mockResponseOnce('{}')
  fetch.mockResponseOnce('ig home page')
  fetch.mockResponseOnce('{}', {status: 400})  // Bridgy returns an HTTP error
  await Instagram.poll()

  expect(fetch.mock.calls.length).toBe(3)
  expect(browser.storage.local.data['instagram-username']).toBeUndefined()
  expect(browser.storage.local.data['instagram-lastStart']).toBeDefined()
  expect(browser.storage.local.data['instagram-lastSuccess']).toBeUndefined()
})

test('poll, existing username stored', async () => {
  await browser.storage.local.set({'instagram-username': 'snarfed'})
  await Instagram.poll()
  expect(fetch.mock.calls[1][0]).toBe(
    'https://i.instagram.com/api/v1/users/web_profile_info/?username=snarfed')
})

test('poll, feed error', async () => {
  fetch.mockResponseOnce('{}')
  fetch.mockResponseOnce('ig feed')
  fetch.mockResponseOnce('{}', {status: 400})  // Bridgy returns an HTTP error

  await browser.storage.local.set({'instagram-username': 'snarfed'})
  await Instagram.poll()

  expect(fetch.mock.calls.length).toBe(3)
  expect(browser.storage.local.data['instagram-lastStart']).toBeDefined()
  expect(browser.storage.local.data['instagram-lastSuccess']).toBeUndefined()
})

test('poll, Bridgy non-JSON response', async () => {
  fetch.mockResponseOnce('{}')
  fetch.mockResponseOnce('ig profile')
  fetch.mockResponseOnce('xyz')  // Bridgy returns invalid JSON

  await browser.storage.local.set({'instagram-username': 'snarfed'})
  await Instagram.poll()

  expect(fetch.mock.calls.length).toBe(3)
  expect(browser.storage.local.data['instagram-lastStart']).toBeDefined()
  expect(browser.storage.local.data['instagram-lastSuccess']).toBeUndefined()
})
