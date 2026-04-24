# 🚀 Deployment Guide for Brevitas Systems

## Prerequisites Checklist
- ✅ `.env.local` is in `.gitignore` (confirmed)
- ✅ API keys are NOT committed to GitHub
- ✅ Rate limiting is implemented
- ✅ Database is set up on Supabase

## 🔐 Security Overview

### Safe to Expose (Public)
- `NEXT_PUBLIC_SUPABASE_URL` - This is meant to be public
- `NEXT_PUBLIC_SUPABASE_ANON_KEY` - This is safe to expose (Row Level Security protects your data)

### MUST Keep Secret
- `SUPABASE_SERVICE_ROLE_KEY` - ⚠️ NEVER expose this! Full database access!

---

## Option 1: Deploy to Vercel (Recommended) ✨

### Why Vercel?
- Free tier includes 100GB bandwidth/month
- Automatic HTTPS/SSL
- Built for Next.js (they make it!)
- Environment variables stay secure
- Automatic deploys from GitHub

### Step-by-Step Deployment

#### 1. Push to GitHub First
```bash
# Make sure .env.local is NOT staged
git status

# Should see .env.local in "Untracked files" or not at all
# If you see it in "Changes to be committed", run:
git rm --cached .env.local

# Add and commit your code
git add .
git commit -m "Add rate limiting and prepare for deployment"
git push origin main
```

#### 2. Deploy to Vercel

1. **Go to [vercel.com](https://vercel.com) and sign up with GitHub**

2. **Click "New Project"**

3. **Import your GitHub repository** (brevitas-systems)

4. **Configure Project:**
   - Framework Preset: Next.js (auto-detected)
   - Root Directory: ./
   - Build Command: `npm run build`
   - Output Directory: .next (default)

5. **Add Environment Variables** (CRITICAL STEP!)

   Click "Environment Variables" and add:

   ```
   NEXT_PUBLIC_SUPABASE_URL = https://ctlhawahnwcfzdikrcxr.supabase.co
   NEXT_PUBLIC_SUPABASE_ANON_KEY = [your anon key]
   SUPABASE_SERVICE_ROLE_KEY = [your service role key - keep secret!]
   ```

6. **Click "Deploy"**

7. **Your site will be live at:** `https://brevitas-systems.vercel.app`

---

## Option 2: Deploy to Netlify (Alternative)

### If you prefer Netlify:

1. **Install Netlify Adapter:**
```bash
npm install @netlify/plugin-nextjs
```

2. **Create `netlify.toml`:**
```toml
[build]
  command = "npm run build"
  publish = ".next"

[[plugins]]
  package = "@netlify/plugin-nextjs"
```

3. **Deploy via Netlify CLI or Web UI**

---

## Option 3: Self-Host on VPS

If you want full control, deploy to:
- DigitalOcean App Platform
- AWS EC2 with PM2
- Google Cloud Run
- Railway.app
- Render.com

---

## ⚠️ Why NOT GitHub Pages?

GitHub Pages limitations:
- ❌ No server-side code execution
- ❌ No API routes
- ❌ No environment variables
- ❌ No database connections
- ❌ No Node.js runtime
- ❌ Only static files (HTML/CSS/JS)

Your app needs:
- ✅ Next.js API routes for `/api/waitlist`
- ✅ Server-side rate limiting
- ✅ Environment variables for Supabase
- ✅ Node.js runtime

---

## 🔒 Post-Deployment Security Checklist

After deployment:

1. **Test Rate Limiting:**
```bash
node test-rate-limiting.js
# Update the URL in the script to your production URL
```

2. **Verify Environment Variables:**
   - Check Vercel dashboard → Settings → Environment Variables
   - Ensure service role key is marked as "Secret"

3. **Monitor Usage:**
   - Set up Vercel Analytics (free)
   - Monitor Supabase dashboard for unusual activity
   - Check rate limiting logs

4. **Add Domain (Optional):**
   - Buy domain from Namecheap/GoDaddy/etc
   - Add to Vercel: Settings → Domains
   - Automatic HTTPS included

---

## 🚨 Emergency: If Keys Get Exposed

If you accidentally commit keys:

1. **Immediately regenerate keys in Supabase:**
   - Settings → API → Regenerate keys

2. **Update in Vercel:**
   - Settings → Environment Variables → Update

3. **Remove from Git history:**
```bash
git filter-branch --tree-filter 'rm -f .env.local' HEAD
git push --force
```

---

## 📊 Free Tier Limits

### Vercel Free Tier:
- 100GB bandwidth/month
- Unlimited deployments
- Automatic HTTPS
- 100,000 function invocations/month

### Supabase Free Tier:
- 500MB database
- 2GB bandwidth
- 50,000 monthly active users
- Unlimited API requests

Both are more than enough for launching your MVP!

---

## Need Help?

- Vercel Docs: https://vercel.com/docs
- Next.js Deployment: https://nextjs.org/docs/deployment
- Supabase Security: https://supabase.com/docs/guides/auth/row-level-security