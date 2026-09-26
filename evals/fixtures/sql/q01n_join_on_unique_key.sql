{% set payment_methods = ['credit_card', 'coupon', 'bank_transfer', 'gift_card'] %}

with orders as (

    select * from {{ ref('stg_orders') }}

),

payments as (

    select * from {{ ref('stg_payments') }}

),

order_payments as (

    select
        order_id,

        {% for payment_method in payment_methods -%}
        sum(case when payment_method = '{{ payment_method }}' then amount else 0 end) as {{ payment_method }}_amount,
        {% endfor -%}

        sum(amount) as total_amount

    from payments

    group by order_id

),

final as (

    select
        orders.order_id,
        orders.customer_id,
        orders.order_date,
        orders.status,

        {% for payment_method in payment_methods -%}

        order_payments.{{ payment_method }}_amount,

        {% endfor -%}

        order_payments.total_amount as amount,
        customers.first_name as customer_first_name

    from orders


    left join order_payments
        on orders.order_id = order_payments.order_id

    left join {{ ref('stg_customers') }} as customers
        on orders.customer_id = customers.customer_id

)

select * from final