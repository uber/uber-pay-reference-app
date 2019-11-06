# Uber payments deposit api - reference app

This application is used to demo, test, and communicate the flow of the uber payments deposit frontend.

## Install 
1. install NodeJS. (v10 lts)
    - `brew install nvm`
    - `nvm install --lts`
    - `nvm set default --lts`
2. install Yarn.
    - `brew install yarn`
3. run `yarn` to install dependencies.
4. copy `.example.env` to `.env` and fill in the configuration properties with your own values.
5. run `npm run start` to run the development environment.
6. open `./app/public/index.html` through Chrome or your preferred browser.

## Creating a private/public keypair.
To receive a redirect from Uber, we ask you to validate our signature to make sure our request are received safely and untampered.

For the reference app you need to get your own keypair. To create a RSA keypair run this command. Make sure to have openssl installed on your machine.
`openssl genrsa -des3 -out private.pem 2048`