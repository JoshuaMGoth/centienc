// PM2 ecosystem file for the CentienC License Server.
// Secrets are NOT stored here — they are loaded from .env by start.sh at runtime.
// Deploy: pm2 start ecosystem.config.js && pm2 save
module.exports = {
  apps: [
    {
      name: 'centienc-license',
      script: './start.sh',
      interpreter: '/bin/bash',
      cwd: '/opt/centienc-license',
      autorestart: true,
      watch: false,
      max_memory_restart: '256M',
      error_file: '/var/log/pm2/centienc-license-error.log',
      out_file: '/var/log/pm2/centienc-license-out.log',
      // PM2 inherits the deploy user's env; all real secrets come from start.sh → .env
      env: {
        NODE_ENV: 'production',
      },
    },
  ],
};
