# ACM cert must be in us-east-1 for CloudFront regardless of region
resource "aws_acm_certificate" "status" {
  provider          = aws.us_east_1
  domain_name       = var.domain
  validation_method = "DNS"
  lifecycle { create_before_destroy = true }
}

resource "aws_route53_record" "cert_validation" {
  for_each = {
    for dvo in aws_acm_certificate.status.domain_validation_options : dvo.domain_name => {
      name   = dvo.resource_record_name
      type   = dvo.resource_record_type
      value  = dvo.resource_record_value
    }
  }
  zone_id = var.route53_zone_id
  name    = each.value.name
  type    = each.value.type
  records = [each.value.value]
  ttl     = 60
}

resource "aws_acm_certificate_validation" "status" {
  provider                = aws.us_east_1
  certificate_arn         = aws_acm_certificate.status.arn
  validation_record_fqdns = [for r in aws_route53_record.cert_validation : r.fqdn]
}

resource "aws_cloudfront_distribution" "status" {
  enabled             = true
  default_root_object = "index.html"
  aliases             = [var.domain]
  price_class         = "PriceClass_100"  # US + EU edges only; cheapest

  # Origin 1: static site files
  origin {
    origin_id                = "site"
    domain_name              = aws_s3_bucket.site.bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.site.id
  }

  # Origin 2: data files (status.json, uptime.json, incidents.json)
  origin {
    origin_id                = "data"
    domain_name              = aws_s3_bucket.data.bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.site.id
  }

  # Default: serve static site
  default_cache_behavior {
    target_origin_id       = "site"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    viewer_protocol_policy = "redirect-to-https"
    compress               = true

    forwarded_values {
      query_string = false
      cookies { forward = "none" }
    }

    min_ttl     = 0
    default_ttl = 60
    max_ttl     = 300
  }

  # /data/* path: serve from data bucket, no caching (status.json changes every minute)
  ordered_cache_behavior {
    path_pattern           = "/data/*"
    target_origin_id       = "data"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    viewer_protocol_policy = "redirect-to-https"
    compress               = true

    forwarded_values {
      query_string = true  # pass ?_=timestamp cache-busting param
      cookies { forward = "none" }
    }

    min_ttl     = 0
    default_ttl = 0
    max_ttl     = 30
  }

  viewer_certificate {
    acm_certificate_arn      = aws_acm_certificate_validation.status.certificate_arn
    ssl_support_method       = "sni-only"
    minimum_protocol_version = "TLSv1.2_2021"
  }

  restrictions {
    geo_restriction { restriction_type = "none" }
  }
}

resource "aws_route53_record" "status" {
  zone_id = var.route53_zone_id
  name    = var.domain
  type    = "A"
  alias {
    name                   = aws_cloudfront_distribution.status.domain_name
    zone_id                = aws_cloudfront_distribution.status.hosted_zone_id
    evaluate_target_health = false
  }
}

# us-east-1 alias provider needed for ACM
provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"
}

output "cloudfront_url" { value = "https://${var.domain}" }
output "cloudfront_distribution_id" { value = aws_cloudfront_distribution.status.id }
