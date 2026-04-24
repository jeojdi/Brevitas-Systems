const { createClient } = require('@supabase/supabase-js');

// Test with the keys you provided
const supabaseUrl = 'https://ctlhawahnwcfzdikrcxr.supabase.co';

// Try different interpretations of the keys you provided
const configs = [
  {
    name: "Current .env.local keys",
    key: process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY
  },
  {
    name: "Your publishable key as-is",
    key: "sb_publishable_OUhsm9ckpUJPuqqhcvyxGQ_3riaXCNJ"
  },
  {
    name: "Your secret key as-is",
    key: "sb_secret_gqbwoFhkCfmNMVMbf4gmLA_R2ABch4Y"
  },
  {
    name: "Publishable without prefix",
    key: "OUhsm9ckpUJPuqqhcvyxGQ_3riaXCNJ"
  },
  {
    name: "Secret without prefix",
    key: "gqbwoFhkCfmNMVMbf4gmLA_R2ABch4Y"
  }
];

async function testConnection(config) {
  console.log(`\nTesting: ${config.name}`);
  console.log(`Key: ${config.key ? config.key.substring(0, 20) + '...' : 'undefined'}`);

  if (!config.key) {
    console.log('❌ No key provided');
    return;
  }

  try {
    const supabase = createClient(supabaseUrl, config.key);

    // Try to fetch from a system table to test the connection
    const { data, error } = await supabase
      .from('waitlist')
      .select('count')
      .limit(1);

    if (error) {
      console.log('❌ Error:', error.message);
    } else {
      console.log('✅ Connection successful!');
    }
  } catch (e) {
    console.log('❌ Exception:', e.message);
  }
}

async function main() {
  console.log('Testing Supabase connections...');
  console.log('Project URL:', supabaseUrl);

  for (const config of configs) {
    await testConnection(config);
  }

  console.log('\n---\nNote: If all tests fail with "Invalid API key", you need to get the full JWT token from your Supabase dashboard.');
  console.log('Go to: https://supabase.com/dashboard/project/ctlhawahnwcfzdikrcxr/settings/api');
  console.log('Copy the full "anon" public key (it should be a very long JWT token starting with "eyJ...")');
}

main().catch(console.error);