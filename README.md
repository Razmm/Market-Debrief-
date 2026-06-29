# Market Debrief Email Automation

This sends a morning and evening market debrief from GitHub Actions, so it can run even when your computer is off.

## Schedule

- Morning: weekdays around 8:30 AM ET
- Evening: weekdays around 5:30 PM ET

GitHub schedules run in UTC, so the workflow includes both daylight-time and standard-time UTC slots. The script only sends during the matching New York hour.

## GitHub Secrets

Add these repository secrets in GitHub:

- `SMTP_USER`: your Gmail address
- `SMTP_PASSWORD`: your Gmail app password
- `EMAIL_TO`: `dylanswaim22@gmail.com`

## Test

After pushing this folder to a GitHub repository, open the `Market debrief email` workflow and choose **Run workflow**. Pick `morning` or `evening`.

