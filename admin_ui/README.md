# Blue Stream Admin UI

A comprehensive React-based admin dashboard for the Blue Stream Water Business Platform.

## Features

### 🎯 Core Functionality
- **Authentication & Authorization**: Secure login with role-based access
- **Real-time Dashboard**: Live metrics and analytics with Chart.js
- **User Management**: Complete user administration with status controls
- **Order Management**: Track and manage orders with advanced filtering
- **Product Management**: Inventory tracking and product catalog management
- **Delivery Management**: Real-time tracking with performance heatmaps
- **Loyalty Program**: Manage rewards and customer engagement
- **Notification System**: Multi-channel communication management
- **Advanced Analytics**: Sales trends, forecasting, and churn prediction

### 📊 Analytics & Business Intelligence
- **Interactive Charts**: Line, Bar, Pie charts with Chart.js
- **Sales Trends**: Revenue tracking and performance analysis
- **Customer Insights**: Churn prediction and behavior analysis
- **Delivery Heatmaps**: Geographic performance visualization
- **Revenue Forecasting**: Predictive analytics for business planning
- **Real-time Updates**: Live data refresh and notifications

### 🎨 User Experience
- **Responsive Design**: Mobile-first approach with Ant Design
- **Dark/Light Theme**: Customizable UI themes
- **Data Export**: CSV, Excel, PDF export capabilities
- **Advanced Filtering**: Multi-criteria search and sorting
- **Bulk Operations**: Efficient multi-item management
- **Real-time Notifications**: Toast messages and alerts

## Tech Stack

### Frontend
- **React 18**: Modern React with hooks and functional components
- **Ant Design 5**: Professional UI component library
- **Chart.js**: Interactive data visualization
- **React Router 6**: Modern routing solution
- **React Query**: Server state management
- **Zustand**: Lightweight state management
- **Axios**: HTTP client with interceptors

### Development Tools
- **TypeScript**: Type safety and better DX
- **ESLint**: Code quality and consistency
- **Prettier**: Code formatting
- **React Hot Toast**: User notifications

## Getting Started

### Prerequisites
- Node.js 16+
- npm or yarn
- Blue Stream API running on localhost:5000

### Installation

1. **Install Dependencies**
   ```bash
   cd admin_ui
   npm install
   ```

2. **Environment Setup**
   ```bash
   cp .env.example .env
   # Edit .env with your API configuration
   ```

3. **Start Development Server**
   ```bash
   npm start
   ```

4. **Build for Production**
   ```bash
   npm run build
   ```

## Project Structure

```
admin_ui/
├── public/
│   ├── index.html
│   └── manifest.json
├── src/
│   ├── components/
│   │   ├── charts/          # Chart components
│   │   ├── common/          # Shared components
│   │   └── layout/          # Layout components
│   ├── pages/
│   │   ├── Dashboard.js     # Main dashboard
│   │   ├── Users.js         # User management
│   │   ├── Orders.js        # Order management
│   │   ├── Products.js      # Product management
│   │   ├── Delivery.js      # Delivery management
│   │   ├── Loyalty.js       # Loyalty program
│   │   ├── Notifications.js # Notification system
│   │   ├── Analytics.js     # Advanced analytics
│   │   └── Settings.js      # System settings
│   ├── services/
│   │   ├── api.js           # API client
│   │   ├── authService.js   # Authentication
│   │   └── adminService.js  # Admin operations
│   ├── stores/
│   │   └── authStore.js     # Auth state management
│   ├── App.js
│   ├── index.js
│   └── index.css
└── package.json
```

## Key Components

### Dashboard
- Real-time metrics and KPIs
- Interactive charts and visualizations
- Quick action buttons and shortcuts
- Customizable date ranges and filters

### User Management
- Advanced search and filtering
- Bulk operations for user management
- Role-based permissions
- Activity tracking and audit logs

### Analytics
- Sales trend analysis
- Customer churn prediction
- Revenue forecasting
- Performance heatmaps
- Export capabilities

## API Integration

The admin UI connects to the Blue Stream API with the following endpoints:

- `GET /api/admin/dashboard` - Dashboard metrics
- `GET /api/admin/users` - User management
- `GET /api/admin/orders` - Order management
- `GET /api/admin/products` - Product management
- `GET /api/admin/analytics` - Analytics data

## Security Features

- JWT-based authentication
- Role-based access control
- API request/response interceptors
- Secure token storage
- Session timeout handling

## Development Guidelines

### Code Style
- Use functional components with hooks
- Follow Ant Design conventions
- Implement proper error handling
- Add loading states for async operations

### State Management
- Use React Query for server state
- Use Zustand for client state
- Minimize prop drilling with context

### Performance
- Implement code splitting
- Use React.memo for expensive components
- Optimize chart rendering
- Implement virtual scrolling for large datasets

## Deployment

### Production Build
```bash
npm run build
```

### Docker Deployment

#### Using Docker Compose (Recommended)
```bash
# From the project root directory
docker-compose up admin_ui
```

#### Standalone Docker Container
```bash
# Build the image
docker build -t bluestream-admin .

# Run the container
docker run -p 3000:80 \
  -e REACT_APP_API_URL=http://localhost:5000/api \
  bluestream-admin
```

#### Development with Docker
```bash
# Start all services including admin UI
docker-compose up

# Access admin UI at http://localhost:3000
# API will be available at http://localhost:5000
```

### Environment Variables
```env
REACT_APP_API_URL=http://localhost:5000/api
REACT_APP_WEBSOCKET_URL=ws://localhost:5000
REACT_APP_TINYMCE_API_KEY=<public-tinymce-key>
```

> **Important — `REACT_APP_*` variables are PUBLIC.** Create-React-App inlines every
> `REACT_APP_*` value into the compiled JS bundle that is shipped to browsers.
> Anyone can read these values from DevTools. Treat them as published identifiers,
> NOT secrets — never put a server-side API key, database credential, or OAuth
> client secret here.
>
> **TinyMCE API key — required hardening:**
>  1. Register the key to BlueStream's TinyMCE account and rotate it if the current
>     value has ever been committed to a public location.
>  2. In the TinyMCE dashboard, enable **domain restriction** so the key only works
>     on our production domains (`aqua-element.uz`, `www.aqua-element.uz`,
>     `admin.aqua-element.uz`) plus `localhost` for dev. This makes the key useless
>     to anyone who scrapes it from our bundle.
>  3. Monitor the TinyMCE quota dashboard; a quota spike is an early sign of abuse
>     even with domain restriction in place.
>
> **Do not** add a backend-only secret (e.g., `PAYME_SECRET_KEY`, `JWT_SECRET_KEY`,
> `SENDGRID_API_KEY`) to `admin_ui/.env` — those belong in the backend secrets
> store (`shared/secrets_manager.py`), not the browser bundle.

## Contributing

1. Follow the established code style
2. Add tests for new features
3. Update documentation
4. Submit pull requests for review

## License

© 2024 Blue Stream Water Business. All rights reserved.
