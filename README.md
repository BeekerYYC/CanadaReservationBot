# Parks Canada Lake O'Hara Cancellation Alerts

Automatically monitors the Parks Canada reservation website (reservation.pc.gc.ca) for cancellations of **Lake O'Hara backcountry camping** permits in Yoho National Park, and sends you an email when a spot opens up.

Uses [camply](https://github.com/juftin/camply) to check availability every 10 minutes via GitHub Actions. No server needed -- runs free on GitHub.

## How It Works

```
GitHub Actions (every 10 min) → camply checks Parks Canada API → email alert if availability found
```

When someone cancels their Lake O'Hara backcountry reservation, camply detects it and emails you immediately so you can grab the spot.

## Setup Guide

### 1. Fork or clone this repository

If you forked it, you're ready. If you cloned it, push it to your own GitHub account.

### 2. Set up a Gmail App Password

You need a Gmail account to send alert emails. If you don't have one, create one at [gmail.com](https://gmail.com).

1. Go to [Google Account Security](https://myaccount.google.com/security)
2. Under "How you sign in to Google", enable **2-Step Verification** (required for App Passwords)
3. Go to [App Passwords](https://myaccount.google.com/apppasswords)
4. Select **"Other (Custom name)"** and enter: `Camply Reservation Bot`
5. Click **Generate**
6. Copy the 16-character password that appears (e.g. `abcd efgh ijkl mnop`)
7. **Save this password** -- you won't be able to see it again

### 3. Add secrets to your GitHub repository

Go to your repository on GitHub, then **Settings > Secrets and variables > Actions**.

Click **"New repository secret"** and add these three secrets:

| Secret Name         | Value                                                                 |
|---------------------|-----------------------------------------------------------------------|
| `EMAIL_TO_ADDRESS`  | Email where you want to receive alerts (can be any email address)     |
| `EMAIL_USERNAME`    | Your Gmail address (e.g. `yourname@gmail.com`)                        |
| `EMAIL_PASSWORD`    | The 16-character App Password from step 2 (no spaces)                 |

### 4. Customize your date range (optional)

Edit `search_config.yaml` to set your preferred travel dates:

```yaml
start_date: "2026-06-19"    # Change to your earliest date
end_date: "2026-10-03"      # Change to your latest date
```

The defaults cover the full 2026 Lake O'Hara season (June 19 - October 3).

### 5. Test it

1. Go to the **Actions** tab in your GitHub repository
2. Click **"Check Lake O'Hara Availability"** in the left sidebar
3. Click **"Run workflow"** > **"Run workflow"**
4. Watch the logs to verify it runs without errors

After that, it will automatically check every 10 minutes.

## Customization

### Change the check frequency

Edit `.github/workflows/check-availability.yml` and change the cron schedule:

```yaml
# Every 5 minutes (more frequent)
- cron: "*/5 * * * *"

# Every 30 minutes (less frequent)
- cron: "*/30 * * * *"
```

### Monitor a different campground

Edit `search_config.yaml` and change the `campgrounds` value. Here are some Yoho IDs:

| Campground                | ID              |
|---------------------------|-----------------|
| Lake O'Hara Backcountry   | `-2147483538`   |
| Lake O'Hara Bus           | `-2147483536`   |
| Kicking Horse Campground  | `-2147483540`   |
| Monarch Campground        | `-2147483539`   |
| Takakkaw Falls Campground | `-2147483522`   |

### Stop the bot

Go to **Actions** > **Check Lake O'Hara Availability** > click the **"..."** menu > **Disable workflow**.

## Troubleshooting

### Workflow isn't running automatically
GitHub disables scheduled workflows after 60 days of no repository activity. Make any small commit to re-enable it, or manually trigger a run from the Actions tab.

### Email not arriving
- Check your spam/junk folder
- Verify the three GitHub secrets are set correctly (no extra spaces)
- Make sure 2-Step Verification is enabled on your Gmail account
- Try regenerating the App Password

### "No campsites found" in logs
This is normal -- it means there are no cancellations right now. The bot will keep checking every 10 minutes.

### 403 or connection errors
Parks Canada uses bot protection (Azure WAF) that may occasionally block requests. If this persists, try reducing the check frequency to every 30 minutes.

## Known Limitations

- **Does not auto-book**: This only sends alerts. You still need to manually book the campsite on reservation.pc.gc.ca once you receive an alert. Cancellations get rebooked very fast, so act quickly.
- **Parks Canada API changes**: If Parks Canada updates their reservation system, camply may need an update. Check [camply releases](https://github.com/juftin/camply/releases) for updates.
- **Public repo = free**: GitHub Actions is free for public repos. For private repos, the 10-minute schedule uses ~4,320 min/month which exceeds the 2,000-minute free tier. Either keep it public or reduce frequency.

## Credits

- [camply](https://github.com/juftin/camply) -- the campsite availability checker
- [Campnab blog post](https://campnab.com/blog/tips-on-getting-backcountry-permit-alerts-at-yoho-national-park) -- inspiration for this project
