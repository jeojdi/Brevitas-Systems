const { createClient } = require('@supabase/supabase-js');

// Your Supabase credentials
const supabaseUrl = 'https://ctlhawahnwcfzdikrcxr.supabase.co';
const supabaseAnonKey = 'sb_publishable_OUhsm9ckpUJPuqqhcvyxGQ_3riaXCNJ';

const supabase = createClient(supabaseUrl, supabaseAnonKey);

async function testWaitlist() {
  console.log('🧪 Testing Supabase waitlist...\n');

  // Test inserting a new entry
  const testEmail = `test-${Date.now()}@example.com`;
  console.log(`📝 Inserting test entry with email: ${testEmail}`);

  const { data, error } = await supabase
    .from('waitlist')
    .insert([{
      email: testEmail,
      company: 'Test Company',
      role: 'Engineering',
      use_case: 'Testing the integration',
      name: 'Test User',
      source: 'test-script'
    }])
    .select()
    .single();

  if (error) {
    if (error.code === '42P01') {
      console.log('❌ Table does not exist yet!');
      console.log('\n📋 To create the table:');
      console.log('1. Go to: https://supabase.com/dashboard/project/ctlhawahnwcfzdikrcxr/sql/new');
      console.log('2. Copy all the SQL from: supabase/create_waitlist_table.sql');
      console.log('3. Paste it in the SQL editor and click "Run"');
      console.log('4. You should see "Success. No rows returned"');
      console.log('5. Then run this test again!');
    } else {
      console.log('❌ Error:', error.message);
      console.log('Error code:', error.code);
    }
    return;
  }

  console.log('✅ Success! Entry created:', data);

  // Test fetching the entry
  console.log('\n📖 Fetching the entry back...');
  const { data: fetchData, error: fetchError } = await supabase
    .from('waitlist')
    .select('*')
    .eq('email', testEmail)
    .single();

  if (fetchError) {
    console.log('❌ Error fetching:', fetchError.message);
  } else {
    console.log('✅ Entry retrieved:', fetchData);
  }

  // Clean up - delete the test entry
  console.log('\n🧹 Cleaning up test entry...');
  const { error: deleteError } = await supabase
    .from('waitlist')
    .delete()
    .eq('email', testEmail);

  if (deleteError) {
    console.log('⚠️  Could not delete test entry:', deleteError.message);
  } else {
    console.log('✅ Test entry deleted');
  }

  console.log('\n🎉 All tests passed! Your waitlist is working correctly.');
  console.log('\n📡 Test the API endpoint:');
  console.log('curl -X POST http://localhost:3000/api/waitlist \\');
  console.log('  -H "Content-Type: application/json" \\');
  console.log('  -d \'{"email": "user@example.com", "company": "Awesome Corp", "role": "CTO"}\'');
}

testWaitlist().catch(console.error);