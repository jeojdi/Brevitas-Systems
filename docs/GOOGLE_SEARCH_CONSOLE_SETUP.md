# Google Search Console Setup Guide

## Domain Verification Steps

### 1. Get Your Verification Token
1. Go to [Google Search Console](https://search.google.com/search-console)
2. Click "Add property"
3. Choose "Domain" property type
4. Enter `brevitassystems.com`
5. Google will provide a TXT record verification token that looks like:
   ```
   google-site-verification=XXXXXXXXXXXXXXXXXXXX
   ```

### 2. Add TXT Record to Your Domain (IONOS)

Since you're using IONOS (based on the SPF record), here's how to add the TXT record:

1. Log in to your [IONOS Control Panel](https://my.ionos.com)
2. Go to **Domains & SSL** → **brevitassystems.com**
3. Click on **DNS Settings** or **Manage DNS**
4. Click **Add Record** → **TXT Record**
5. Add the following:
   - **Host/Name**: `@` (or leave blank for root domain)
   - **Value/Text**: `google-site-verification=XXXXXXXXXXXXXXXXXXXX` (paste your actual token)
   - **TTL**: 3600 (or default)
6. Save the record

### 3. Verify in Google Search Console
1. Wait 5-10 minutes for DNS propagation
2. Go back to Google Search Console
3. Click "Verify" button
4. Once verified, you'll have access to Search Console features

## Current DNS Records

Your domain currently has:
```
TXT @ "v=spf1 include:_spf-us.ionos.com ~all"
```

After adding Google verification, you'll have:
```
TXT @ "v=spf1 include:_spf-us.ionos.com ~all"
TXT @ "google-site-verification=XXXXXXXXXXXXXXXXXXXX"
```

## Alternative Verification Methods

If DNS verification doesn't work, you can try:

### HTML File Upload (Recommended Alternative)
1. Download the HTML verification file from Google
2. Add it to the `/public` folder in your Next.js project
3. Deploy to Vercel
4. Google will check `https://brevitassystems.com/googleXXXXXXXX.html`

### HTML Tag Method
Add this to your site's `<head>`:
```html
<meta name="google-site-verification" content="XXXXXXXXXXXXXXXXXXXX" />
```

## After Verification

Once verified, you should:
1. Submit your sitemap: `https://brevitassystems.com/sitemap.xml`
2. Check for crawl errors
3. Monitor search performance
4. Set up email notifications for issues

## Troubleshooting

### DNS Not Propagating?
- Check propagation status at [whatsmydns.net](https://www.whatsmydns.net/)
- Use DNS lookup: `nslookup -type=txt brevitassystems.com`

### Multiple TXT Records?
IONOS allows multiple TXT records. Your SPF record won't interfere with Google verification.

### Still Not Working?
- Ensure there are no typos in the verification code
- Try clearing DNS cache: `sudo dscacheutil -flushcache` (macOS)
- Contact IONOS support if DNS changes aren't saving

## Important Notes

- Keep the TXT record even after verification to maintain ownership
- You can add multiple verification records for different Google services
- The verification token is unique to your Google account

---

**Last Updated**: April 25, 2026
**Next Steps**: Add TXT record to IONOS DNS settings