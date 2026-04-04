// PM2 ecosystem file for the CentienC monitoring service (10.10.10.221).
// Copy to /var/lib/centienc/ecosystem.config.js on the server, then:
//   pm2 start /var/lib/centienc/ecosystem.config.js
//   pm2 save
//   pm2 startup   ← follow the printed instructions to enable auto-start
module.exports = {
  apps: [
    {
      name: 'centienc',
      script: '/opt/centienc/venv/bin/python',
      args: '-m centient --host 0.0.0.0 --port 9099',
      interpreter: 'none',
      cwd: '/var/lib/centienc',
      autorestart: true,
      watch: false,
      max_memory_restart: '512M',
      error_file: '/var/log/pm2/centienc-error.log',
      out_file: '/var/log/pm2/centienc-out.log',
      env: {
        CENTIENT_DATA_DIR: '/var/lib/centienc',
      },
    },
  ],
};
