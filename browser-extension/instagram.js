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
      username = await this.forward('/', '/homepage')
      if (!username) {
        return
      }
      await this.storageSet('username', username)
    }

    return `/${username}/`
  }

  /**
   * Returns the URL path to the user's feed of posts.
   */
  static async feedPath() {
    return await this.profilePath()
  }

  /**
   * Returns an AS activity's like count, if available.
   */
  static reactionsCount(activity) {
    return activity.object.ig_like_count
  }

  /**
   * Returns the URL path for a given activity's likes.
   */
  static reactionsPath(activity) {
    return `/graphql/query/?query_hash=d5d763b1e2acf209d62d22d184488e57&variables={"shortcode":"${activity.object.ig_shortcode}","include_reel":false,"first":100}`
  }

  /**
   * Returns the URL path for a given activity's comments.
   */
  static commentsPath(activity) {
    const id = activity.id.split(':')[2].split('_')[0]
    return `https://i.instagram.com/api/v1/media/${id}/comments/?can_support_threading=true&permalink_enabled=false`
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
Instagram.BASE_URL = 'https://www.instagram.com'
Instagram.LOGIN_URL = `${Instagram.BASE_URL}/accounts/login/`
Instagram.COOKIE = 'sessionid'

export {
  Instagram,
}
