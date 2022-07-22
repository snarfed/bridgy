'use strict'

import {Silo} from './common.js'


class Instagram extends Silo {
  /** See below class declaration for class static properties. */

  /**
   * Returns the URL path to the user's profile, eg '/snarfed/'.
   */
  static async profilePath() {
    let username = await this.storageGet('username')

    if (!username) {
      // extract username from a logged in home page fetch
      username = await this.forward('https://www.instagram.com/', '/homepage')
      if (!username) {
        return
      }
      await this.storageSet('username', username)
    }

    return `/api/v1/users/web_profile_info/?username=${username}`
  }

  /**
   * Returns the URL path to the user's feed of posts.
   */
  static async feedPath() {
    return await this.profilePath()
  }

  /**
   * Returns the URL to scrape for a post.
   *
   * @param {Object} AS1 activity of the post
   */
  static postURL(activity) {
    // id will be a tag URI, eg tag:instagram.com:123
    const id = activity.id.split(':').at(-1)
    return `${Instagram.BASE_URL}/api/v1/media/${id}/info/`
  }

  /**
   * Returns an AS activity's like count, if available.
   */
  static reactionsCount(activity) {
    return activity.object.ig_like_count
  }

  /**
   * Returns HTTP headers to include in silo requests.
   */
  static headers() {
    return {
      // duplicated in granary/instagram.py
      'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:96.0) Gecko/20100101 Firefox/96.0',
      'X-IG-App-ID': '936619743392459',  // desktop web
    }
  }
}

Instagram.DOMAIN = 'instagram.com'
Instagram.NAME = 'instagram'
Instagram.BASE_URL = 'https://i.instagram.com'
Instagram.LOGIN_URL = `https://www.instagram.com/accounts/login/`
Instagram.COOKIE = 'sessionid'

export {
  Instagram,
}
