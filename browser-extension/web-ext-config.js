// https://extensionworkshop.com/documentation/develop/getting-started-with-web-ext/#setting-option-defaults-in-a-configuration-file
module.exports = {
  build: {
    overwriteDest: true,
  },
  ignoreFiles: [
    'babel.config.json',
    'package-lock.json',
    'screenshot*',
    'test',
    'web-ext-artifacts',
    'yarn.lock',
    '*.xpi',
    '*.zip',
  ],
}
