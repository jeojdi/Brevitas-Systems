# Supabase Setup Instructions

## 📋 Quick Setup Guide

Follow these steps to connect your Brevitas Systems website to Supabase:

### Step 1: Get Your Supabase API Keys 🔑

1. Go to your Supabase dashboard: https://supabase.com/dashboard/project/ctlhawahnwcfzdikrcxr
2. Click on **"API Keys"** (key icon) in the left sidebar
3. Copy the **"anon public"** key
4. Open `.env.local` file in your project
5. Replace `your-anon-key-here` with the actual key you copied

### Step 2: Create the Waitlist Table 📊

1. In your Supabase dashboard, click on **"SQL Editor"** (database icon) in the left sidebar
2. Click **"New query"**
3. Copy and paste the entire contents of `supabase/create_waitlist_table.sql`
4. Click **"Run"** to execute the SQL
5. You should see a success message saying the table was created

### Step 3: Restart Your Development Server 🔄

```bash
# Stop the current server (Ctrl+C) and restart it
cd /Users/james/Documents/GitHub/Brevitas-Systems/brevitas-systems
npm run dev
```

### Step 4: Test the Integration ✅

1. Open your browser to http://localhost:3000/test.html
2. Click the **"Test Waitlist API"** button
3. You should see a success message

Or test via command line:
```bash
curl -X POST http://localhost:3000/api/waitlist \
  -H "Content-Type: application/json" \
  -d '{"email": "test@example.com", "company": "Test Corp", "role": "Engineering", "use_case": "Testing integration"}'
```

### Step 5: View Your Data 👀

1. Go back to your Supabase dashboard
2. Click on **"Table Editor"** in the left sidebar
3. Select the **"waitlist"** table
4. You should see your test entries!

## 🎉 Success!

Your waitlist is now connected to Supabase! Every form submission will be:
- ✅ Validated for proper email format
- ✅ Saved to your Supabase database
- ✅ Protected against duplicate emails
- ✅ Timestamped automatically

## 📝 What's Included

### Database Features:
- **Automatic timestamps**: `created_at` and `updated_at` fields
- **Email uniqueness**: Prevents duplicate signups
- **Row Level Security**: Configured for public inserts, authenticated reads
- **Indexed fields**: Fast lookups on email and created_at

### API Features:
- **POST /api/waitlist**: Add new signup
- **GET /api/waitlist?email=test@example.com**: Check if email exists
- **Graceful fallback**: Works without Supabase (logs to console)
- **Error handling**: Proper status codes and messages

## 🔧 Optional: Admin View

To create an admin page to view waitlist entries:

1. Create an authenticated user in Supabase (Settings → Authentication → Users)
2. Build an admin page that uses Supabase auth
3. Query the waitlist table with authenticated access

## 🚀 Next Steps

Consider adding:
- Email notifications (SendGrid, Resend, etc.)
- Slack/Discord notifications for new signups
- Export to CSV functionality
- Analytics dashboard
- Welcome email automation

## 🐛 Troubleshooting

### "Missing Supabase environment variables" error
- Make sure `.env.local` file exists and has both URL and ANON_KEY
- Restart your development server after adding environment variables

### "Failed to join waitlist" error
- Check that the waitlist table was created successfully
- Verify your API key is correct
- Check Supabase dashboard for any database errors

### Duplicate email errors
- This is expected behavior - each email can only sign up once
- To allow re-signup, delete the entry from Supabase Table Editor

## 📚 Resources

- [Supabase Documentation](https://supabase.com/docs)
- [Next.js Environment Variables](https://nextjs.org/docs/basic-features/environment-variables)
- [Supabase JavaScript Client](https://supabase.com/docs/reference/javascript/introduction)