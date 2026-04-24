#!/usr/bin/env node

// Test script to verify form submission works after table creation

async function testFormSubmission() {
  const testData = {
    email: `test-${Date.now()}@example.com`,
    name: 'Test User',
    company: 'Test Company',
    role: 'Developer',
    use_case: 'Testing the waitlist form submission',
    source: 'test-script'
  };

  console.log('🧪 Testing form submission to http://localhost:3000/api/waitlist');
  console.log('📧 Test email:', testData.email);

  try {
    const response = await fetch('http://localhost:3000/api/waitlist', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(testData),
    });

    const result = await response.json();

    if (response.ok) {
      console.log('✅ Form submission successful!');
      console.log('📊 Response:', result);
      console.log('\n🎉 Your waitlist form is now fully working!');
      console.log('📍 Check your Supabase dashboard to see the entry:');
      console.log('   https://supabase.com/dashboard/project/ctlhawahnwcfzdikrcxr/editor/waitlist');
    } else {
      console.error('❌ Submission failed:', result);
      if (result.error?.includes('relation "public.waitlist" does not exist')) {
        console.log('\n⚠️  The waitlist table doesn\'t exist yet.');
        console.log('📋 Please create it first:');
        console.log('   1. Go to: https://supabase.com/dashboard/project/ctlhawahnwcfzdikrcxr/sql/new');
        console.log('   2. Copy the SQL from supabase/create_waitlist_table.sql');
        console.log('   3. Paste and click "Run"');
      }
    }
  } catch (error) {
    console.error('❌ Error testing form:', error.message);
    console.log('\n⚠️  Make sure your dev server is running:');
    console.log('   npm run dev');
  }
}

testFormSubmission();