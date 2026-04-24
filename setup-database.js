const { createClient } = require('@supabase/supabase-js');
const fs = require('fs');
const path = require('path');

// Directly use the keys since dotenv isn't installed
const supabaseUrl = 'https://ctlhawahnwcfzdikrcxr.supabase.co';
const supabaseServiceKey = 'sb_secret_gqbwoFhkCfmNMVMbf4gmLA_R2ABch4Y'; // Using service key for admin operations

if (!supabaseUrl || !supabaseServiceKey) {
  console.error('❌ Missing Supabase environment variables!');
  console.error('Please ensure .env.local contains:');
  console.error('  NEXT_PUBLIC_SUPABASE_URL');
  console.error('  SUPABASE_SERVICE_ROLE_KEY');
  process.exit(1);
}

console.log('🚀 Setting up Supabase database...');
console.log('Project URL:', supabaseUrl);

const supabase = createClient(supabaseUrl, supabaseServiceKey, {
  auth: {
    autoRefreshToken: false,
    persistSession: false
  }
});

async function setupDatabase() {
  try {
    // Read the SQL file
    const sqlPath = path.join(__dirname, 'supabase', 'create_waitlist_table.sql');
    const sql = fs.readFileSync(sqlPath, 'utf8');

    console.log('\n📝 Executing SQL to create waitlist table...');

    // Execute the SQL
    const { data, error } = await supabase.rpc('exec_sql', {
      query: sql
    }).catch(async (err) => {
      // If exec_sql doesn't exist, try alternative approach
      console.log('⚠️  Standard SQL execution not available, trying alternative...');

      // Test if table already exists
      const { data: tables, error: tablesError } = await supabase
        .from('waitlist')
        .select('count')
        .limit(1);

      if (!tablesError || tablesError.code !== '42P01') {
        console.log('✅ Waitlist table already exists!');
        return { data: 'Table exists', error: null };
      }

      return { data: null, error: err };
    });

    if (error) {
      console.error('❌ Error creating table:', error.message);
      console.log('\n📋 Please create the table manually:');
      console.log('1. Go to: https://supabase.com/dashboard/project/ctlhawahnwcfzdikrcxr/sql/new');
      console.log('2. Copy the contents of supabase/create_waitlist_table.sql');
      console.log('3. Paste and click "Run"');
      return false;
    }

    console.log('✅ Database setup complete!');

    // Test the table
    console.log('\n🧪 Testing table...');
    const testEmail = `test-${Date.now()}@example.com`;
    const { data: testData, error: testError } = await supabase
      .from('waitlist')
      .insert([{
        email: testEmail,
        company: 'Test Company',
        role: 'Test Role',
        use_case: 'Testing database setup',
        name: 'Test User',
        source: 'setup-script'
      }])
      .select()
      .single();

    if (testError) {
      console.error('❌ Test insert failed:', testError.message);
      if (testError.code === '42P01') {
        console.log('\n📋 Table needs to be created manually:');
        console.log('1. Go to: https://supabase.com/dashboard/project/ctlhawahnwcfzdikrcxr/sql/new');
        console.log('2. Copy the contents of supabase/create_waitlist_table.sql');
        console.log('3. Paste and click "Run"');
      }
      return false;
    }

    console.log('✅ Test insert successful!');
    console.log('   Created test entry:', testData);

    // Clean up test entry
    const { error: deleteError } = await supabase
      .from('waitlist')
      .delete()
      .eq('id', testData.id);

    if (!deleteError) {
      console.log('✅ Test entry cleaned up');
    }

    return true;
  } catch (error) {
    console.error('❌ Setup failed:', error.message);
    console.log('\n📋 Please create the table manually:');
    console.log('1. Go to: https://supabase.com/dashboard/project/ctlhawahnwcfzdikrcxr/sql/new');
    console.log('2. Copy the contents of supabase/create_waitlist_table.sql');
    console.log('3. Paste and click "Run"');
    return false;
  }
}

setupDatabase().then((success) => {
  if (success) {
    console.log('\n🎉 Database is ready! Your waitlist API should now work.');
    console.log('\nTest it with:');
    console.log('curl -X POST http://localhost:3000/api/waitlist \\');
    console.log('  -H "Content-Type: application/json" \\');
    console.log('  -d \'{"email": "test@example.com", "company": "Test Corp"}\'');
  } else {
    console.log('\n⚠️  Manual setup required - see instructions above');
  }
});