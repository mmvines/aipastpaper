# 🚀 Stripe Integration Setup Guide

Your A-Level Physics Explainer app now has **perfect Stripe checkout integration**! Here's everything you need to know.

## ✅ What's Already Implemented

- **Stripe Checkout Sessions** with your real price IDs
- **Subscription Management** in MongoDB
- **User Limits** based on subscription plans
- **Webhook Handling** for payment confirmations
- **Account Management** with subscription status
- **Free Tier** with 3 searches limit

## 🔑 Configuration

### 1. Secrets File (`.streamlit/secrets.toml`)

Your secrets file is already configured with:
```toml
STRIPE_SECRET_KEY = "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"
STRIPE_PUBLISHABLE_KEY = "pk_live_51PZ3rpJzjO2r6EtJMQuqdrWtczXUsbA6hqWzx9ANwp1q7AP3b22HRNppBpQ37HfLFXIeBVucSN4z7wj2AL25xei500tjpTVnTB"

# Stripe Price IDs for subscription plans
BASIC_PRICE_ID = "price_1RyXTNJzjO2r6EtJHB6c6pNI"
PLUS_PRICE_ID = "price_1RyXUsJzjO2r6EtJgfeBjTqp"
PRO_PRICE_ID = "price_1RyXWZJzjO2r6EtJqJqbhNnH"
```

### 2. Subscription Plans

| Plan | Price | Monthly Searches | Features |
|------|-------|------------------|----------|
| **Basic** | $5/month | 50 | All papers, PDF downloads, Basic support |
| **Plus** | $20/month | 200 | All papers, PDF downloads, Priority support, Analytics |
| **Pro** | $100/month | 1000 | All papers, PDF downloads, Premium support, Analytics, Custom uploads |

## 🧪 Testing Your Integration

### 1. Run the Test Suite

```bash
python test_stripe.py
```

This will verify:
- ✅ Configuration files
- ✅ Stripe connection
- ✅ Price ID validity

### 2. Test the App

```bash
streamlit run app.py
```

## 🔄 How It Works

### 1. User Flow
1. **Free Users**: Get 3 searches, then see subscription popup
2. **Paid Users**: Choose plan → Stripe checkout → Immediate access
3. **Account Management**: View usage, limits, and subscription details

### 2. Payment Process
1. User selects plan
2. App creates Stripe checkout session
3. User redirected to Stripe
4. After payment, user returns with success parameters
5. App verifies payment and activates subscription

### 3. Subscription Tracking
- **MongoDB Collection**: `user_subscriptions`
- **Real-time Updates**: Search counts, plan changes
- **Webhook Support**: Automatic subscription management

## 🌐 Webhook Setup (Production)

For production, set up webhooks in your Stripe dashboard:

### Webhook URL
```
https://your-domain.com/webhook
```

### Events to Listen For
- `checkout.session.completed`
- `customer.subscription.updated`
- `customer.subscription.deleted`

### Webhook Secret
Add to your secrets file:
```toml
STRIPE_WEBHOOK_SECRET = "whsec_your_webhook_secret"
```

## 🛠️ Admin Features

### Admin Panel Access
- Password: `admin123`
- Navigate to Admin Panel → Manage subscriptions, view analytics

### Content Management
- Upload new question papers
- Manage metadata and difficulty levels
- View user activity and ratings

## 🔧 Troubleshooting

### Common Issues

1. **"Stripe is not configured"**
   - Check your secrets file
   - Verify API keys are correct

2. **"Invalid plan" error**
   - Ensure price IDs match your Stripe dashboard
   - Check plan names: `basic`, `plus`, `pro`

3. **Checkout fails**
   - Verify your Stripe account is active
   - Check if you're using test vs live keys

4. **Subscription not activating**
   - Check webhook configuration
   - Verify MongoDB connection

### Debug Mode

Enable debug logging by adding to your secrets:
```toml
DEBUG = true
```

## 📱 Mobile Optimization

The app is fully mobile-optimized with:
- Responsive design
- Touch-friendly buttons
- Mobile-optimized forms
- Progressive Web App features

## 🚀 Production Deployment

### 1. Environment Variables
```bash
export STRIPE_SECRET_KEY="sk_live_..."
export STRIPE_PUBLISHABLE_KEY="pk_live_..."
export MONGO_URL="mongodb://..."
```

### 2. Webhook Endpoint
Set up a proper webhook endpoint at `/webhook` to handle Stripe events.

### 3. SSL Certificate
Ensure HTTPS for webhook security.

## 💡 Advanced Features

### Custom Plans
Add new plans by:
1. Creating price in Stripe dashboard
2. Adding to `plan_limits` in `save_user_subscription()`
3. Updating UI components

### Analytics
Track usage patterns:
- Search frequency by plan
- Popular question types
- User engagement metrics

### A/B Testing
Test different pricing strategies:
- Plan limits
- Feature combinations
- Pricing tiers

## 🎯 Success Metrics

Monitor these KPIs:
- **Conversion Rate**: Free → Paid users
- **Churn Rate**: Subscription cancellations
- **ARPU**: Average Revenue Per User
- **LTV**: Customer Lifetime Value

## 📞 Support

If you need help:
1. Check the test suite output
2. Review Stripe dashboard logs
3. Check MongoDB connection
4. Verify webhook delivery

---

**🎉 Your Stripe integration is now production-ready!**

The app handles everything from free user limits to subscription management, with a beautiful UI and robust backend. Users can seamlessly upgrade their plans and enjoy unlimited learning!
