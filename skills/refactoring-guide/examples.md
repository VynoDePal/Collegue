# Exemples de refactoring — Avant / Après

## Extract Method (Python)

### Avant
```python
def process_order(order):
    # Validate
    if not order.items:
        raise ValueError("Empty order")
    if order.total < 0:
        raise ValueError("Invalid total")
    for item in order.items:
        if item.quantity <= 0:
            raise ValueError(f"Invalid quantity for {item.name}")

    # Calculate
    subtotal = sum(item.price * item.quantity for item in order.items)
    tax = subtotal * 0.2
    shipping = 5.99 if subtotal < 50 else 0
    total = subtotal + tax + shipping

    # Save
    order.subtotal = subtotal
    order.tax = tax
    order.shipping = shipping
    order.total = total
    order.status = "processed"
    db.save(order)

    # Notify
    email.send(order.customer.email, f"Order {order.id} confirmed")
    analytics.track("order_processed", {"total": total})

    return order
```

### Après
```python
def process_order(order: Order) -> Order:
    _validate_order(order)
    _calculate_totals(order)
    _save_order(order)
    _notify_order_processed(order)
    return order

def _validate_order(order: Order) -> None:
    if not order.items:
        raise ValueError("Empty order")
    if order.total < 0:
        raise ValueError("Invalid total")
    for item in order.items:
        if item.quantity <= 0:
            raise ValueError(f"Invalid quantity for {item.name}")

def _calculate_totals(order: Order) -> None:
    order.subtotal = sum(
        item.price * item.quantity for item in order.items
    )
    order.tax = order.subtotal * TAX_RATE
    order.shipping = (
        SHIPPING_COST if order.subtotal < FREE_SHIPPING_THRESHOLD else 0
    )
    order.total = order.subtotal + order.tax + order.shipping

def _save_order(order: Order) -> None:
    order.status = "processed"
    db.save(order)

def _notify_order_processed(order: Order) -> None:
    email.send(order.customer.email, f"Order {order.id} confirmed")
    analytics.track("order_processed", {"total": order.total})
```

## Strategy Pattern (TypeScript)

### Avant
```typescript
function calculateDiscount(type: string, amount: number): number {
  if (type === 'percentage') {
    return amount * 0.1
  } else if (type === 'fixed') {
    return 10
  } else if (type === 'bogo') {
    return amount / 2
  } else if (type === 'loyalty') {
    return amount * 0.15
  } else if (type === 'seasonal') {
    return amount * 0.2
  }
  return 0
}
```

### Après
```typescript
interface DiscountStrategy {
  calculate(amount: number): number
}

const discountStrategies: Record<string, DiscountStrategy> = {
  percentage: { calculate: (amount) => amount * 0.1 },
  fixed: { calculate: () => 10 },
  bogo: { calculate: (amount) => amount / 2 },
  loyalty: { calculate: (amount) => amount * 0.15 },
  seasonal: { calculate: (amount) => amount * 0.2 },
}

function calculateDiscount(type: string, amount: number): number {
  const strategy = discountStrategies[type]
  if (!strategy) return 0
  return strategy.calculate(amount)
}
```

## Simplify Conditionals (Python)

### Avant
```python
def get_user_status(user):
    if user is not None:
        if user.is_active:
            if user.subscription is not None:
                if user.subscription.is_valid():
                    if user.subscription.plan == 'premium':
                        return 'premium_active'
                    else:
                        return 'basic_active'
                else:
                    return 'subscription_expired'
            else:
                return 'no_subscription'
        else:
            return 'inactive'
    else:
        return 'unknown'
```

### Après (Guard Clauses)
```python
def get_user_status(user: User | None) -> str:
    if user is None:
        return 'unknown'
    if not user.is_active:
        return 'inactive'
    if user.subscription is None:
        return 'no_subscription'
    if not user.subscription.is_valid():
        return 'subscription_expired'
    if user.subscription.plan == 'premium':
        return 'premium_active'
    return 'basic_active'
```

## Replace Callback Hell with Async/Await (JavaScript)

### Avant
```javascript
function fetchUserData(userId, callback) {
  getUser(userId, (err, user) => {
    if (err) return callback(err)
    getOrders(user.id, (err, orders) => {
      if (err) return callback(err)
      getPayments(orders[0].id, (err, payments) => {
        if (err) return callback(err)
        callback(null, { user, orders, payments })
      })
    })
  })
}
```

### Après
```javascript
async function fetchUserData(userId) {
  const user = await getUser(userId)
  const orders = await getOrders(user.id)
  const payments = await getPayments(orders[0].id)
  return { user, orders, payments }
}
```

## Dependency Injection (TypeScript)

### Avant (couplage direct)
```typescript
class OrderService {
  private db = new PostgresDatabase()
  private mailer = new SendGridMailer()
  private logger = new WinstonLogger()

  async createOrder(data: OrderData): Promise<Order> {
    const order = await this.db.insert('orders', data)
    await this.mailer.send(data.email, 'Order confirmed')
    this.logger.info(`Order ${order.id} created`)
    return order
  }
}
```

### Après (injection de dépendances)
```typescript
interface Database {
  insert(table: string, data: unknown): Promise<unknown>
}

interface Mailer {
  send(to: string, subject: string): Promise<void>
}

interface Logger {
  info(message: string): void
}

class OrderService {
  constructor(
    private readonly db: Database,
    private readonly mailer: Mailer,
    private readonly logger: Logger,
  ) {}

  async createOrder(data: OrderData): Promise<Order> {
    const order = await this.db.insert('orders', data)
    await this.mailer.send(data.email, 'Order confirmed')
    this.logger.info(`Order ${order.id} created`)
    return order
  }
}

// Testable : on peut injecter des mocks
const service = new OrderService(mockDb, mockMailer, mockLogger)
```

## Modernize Python (dataclass + pathlib)

### Avant
```python
import os

class Config:
    def __init__(self, name, host, port, debug=False):
        self.name = name
        self.host = host
        self.port = port
        self.debug = debug

    def __repr__(self):
        return "Config(name={}, host={}, port={})".format(
            self.name, self.host, self.port
        )

def load_config(config_dir):
    config_path = os.path.join(config_dir, 'config.json')
    if not os.path.exists(config_path):
        raise FileNotFoundError(
            "Config not found: {}".format(config_path)
        )
    with open(config_path, 'r') as f:
        data = json.load(f)
    return Config(
        data['name'], data['host'], data['port'],
        data.get('debug', False)
    )
```

### Après
```python
from dataclasses import dataclass
from pathlib import Path

@dataclass(frozen=True)
class Config:
    name: str
    host: str
    port: int
    debug: bool = False

def load_config(config_dir: Path) -> Config:
    config_path = config_dir / 'config.json'
    if not config_path.exists():
        raise FileNotFoundError(f"Config not found: {config_path}")
    data = json.loads(config_path.read_text())
    return Config(
        name=data['name'],
        host=data['host'],
        port=data['port'],
        debug=data.get('debug', False),
    )
```
